import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

from app.main import CurrentUser, Role, app, get_current_user


class PatientCorrectionRequestEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The duplicate pre-check is covered in test_patient_correction_history.
        self._precheck = patch(
            "app.main._blocking_correction_request_id",
            new=AsyncMock(return_value=None),
        )
        self._precheck.start()

    async def asyncTearDown(self):
        self._precheck.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def _set_current_user(self, role=Role.PATIENT, user_id=42, permissions=None):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id,
            email="user@example.test",
            role=role,
            permissions=permissions or [],
        )

    async def _post(self, body):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.post("/api/patient/correction-requests", json=body)

    async def test_unauthenticated_request_is_rejected(self):
        with patch("app.main._request_supabase", new=AsyncMock()) as request:
            response = await self._post({"proposed_treatment": "Fictional treatment"})

        self.assertEqual(response.status_code, 401, response.text)
        request.assert_not_awaited()

    async def test_non_patient_roles_are_rejected_without_rpc(self):
        with patch("app.main._request_supabase", new=AsyncMock()) as request:
            for role in (Role.ADMIN, Role.PHYSICIAN, Role.TECHNICIAN):
                self._set_current_user(
                    role,
                    permissions=["submit_record_correction"],
                )
                with self.subTest(role=role.value):
                    response = await self._post(
                        {"proposed_treatment": "Fictional treatment"}
                    )

                self.assertEqual(response.status_code, 403, response.text)

        request.assert_not_awaited()

    async def test_patient_permission_is_required_before_rpc(self):
        self._set_current_user(permissions=[])
        with patch("app.main._request_supabase", new=AsyncMock()) as request:
            response = await self._post(
                {"proposed_treatment": "Fictional treatment"}
            )

        self.assertEqual(response.status_code, 403, response.text)
        request.assert_not_awaited()

    async def test_unknown_fields_and_invalid_proposals_are_rejected_safely(self):
        self._set_current_user(permissions=["submit_record_correction"])
        invalid_bodies = (
            {},
            {"proposed_treatment": "   "},
            {"proposed_treatment": "x" * 2001},
            {
                "proposed_treatment": "Fictional treatment",
                "patient_id": 101,
            },
            {
                "proposed_treatment": "Fictional treatment",
                "user_id": 99,
            },
            {
                "proposed_treatment": "Fictional treatment",
                "reviewer_id": 4,
                "approval_status": "Approved",
                "guardrail_decision": "Allow",
            },
            {"proposed_treatment": 42},
        )

        responses = []
        with patch("app.main._request_supabase", new=AsyncMock()) as rpc:
            for body in invalid_bodies:
                with self.subTest(body_keys=tuple(body)):
                    response = await self._post(body)
                    self.assertEqual(response.status_code, 422, response.text)
                    responses.append(response)

        rpc.assert_not_awaited()
        for response in responses:
            self.assertNotIn("x" * 2001, response.text)

    async def test_rpc_uses_authenticated_user_and_service_role_and_returns_safe_metadata(self):
        self._set_current_user(
            user_id=42,
            permissions=["submit_record_correction"],
        )
        rpc_result = {
            "request_id": 731,
            "guardrail_decision": "Escalate",
            "approval_status": "Pending",
            "patient_id": 101,
            "request_details": "must not be returned",
        }
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(return_value=httpx.Response(200, json=rpc_result)),
            ) as request,
        ):
            response = await self._post(
                {"proposed_treatment": "Fictional proposed treatment"}
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "request_id": 731,
                "guardrail_decision": "Escalate",
                "approval_status": "Pending",
            },
        )
        request.assert_awaited_once()
        rpc_call = request.await_args
        if rpc_call is None:
            self.fail("Supabase RPC was not called.")
        self.assertEqual(
            rpc_call.args[0],
            "/rest/v1/rpc/submit_patient_treatment_correction",
        )
        self.assertEqual(rpc_call.kwargs["method"], "POST")
        self.assertEqual(
            rpc_call.kwargs["json_body"],
            {
                "p_requested_by": 42,
                "p_proposed_treatment": "Fictional proposed treatment",
            },
        )
        self.assertEqual(rpc_call.kwargs["api_key"], "server-only-key")
        self.assertEqual(
            rpc_call.kwargs["extra_headers"],
            {"Authorization": "Bearer server-only-key"},
        )
        self.assertNotIn("patient_id", response.json())
        self.assertNotIn("request_details", response.json())

    async def test_rpc_invalid_proposal_is_reported_without_database_details(self):
        self._set_current_user(permissions=["submit_record_correction"])
        sensitive_text = "Fictional private clinical details"
        rpc_error = httpx.Response(
            400,
            json={
                "code": "22023",
                "message": f"Invalid proposal: {sensitive_text}",
            },
        )
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(return_value=rpc_error),
            ),
        ):
            response = await self._post(
                {"proposed_treatment": "Fictional proposed treatment"}
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertNotIn(sensitive_text, response.text)

    async def test_missing_authorized_physician_returns_safe_conflict(self):
        self._set_current_user(permissions=["submit_record_correction"])
        rpc_error = httpx.Response(
            403,
            json={
                "code": "42501",
                "message": (
                    "No unique active assigned Physician is authorized to review this request."
                ),
            },
        )
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(return_value=rpc_error),
            ),
        ):
            response = await self._post(
                {"proposed_treatment": "Fictional proposed treatment"}
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertNotIn("42501", response.text)

    async def test_rpc_authorization_denial_is_safe(self):
        self._set_current_user(permissions=["submit_record_correction"])
        rpc_error = httpx.Response(
            403,
            json={
                "code": "42501",
                "message": "Sensitive database authorization details",
            },
        )
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(return_value=rpc_error),
            ),
        ):
            response = await self._post(
                {"proposed_treatment": "Fictional proposed treatment"}
            )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertNotIn("Sensitive database authorization details", response.text)

    async def test_supabase_and_transport_errors_do_not_expose_details(self):
        self._set_current_user(permissions=["submit_record_correction"])
        sensitive_text = "server-only-key and fictional clinical details"
        failure_responses = (
            httpx.Response(500, text=sensitive_text),
            HTTPException(status_code=503, detail=sensitive_text),
            httpx.HTTPError(sensitive_text),
        )

        for failure in failure_responses:
            with self.subTest(failure_type=type(failure).__name__):
                with (
                    patch(
                        "app.main._service_role_key",
                        return_value="server-only-key",
                    ),
                    patch(
                        "app.main._request_supabase",
                        new=AsyncMock(
                            side_effect=failure
                            if isinstance(failure, Exception)
                            else None,
                            return_value=(
                                failure
                                if isinstance(failure, httpx.Response)
                                else None
                            ),
                        ),
                    ),
                ):
                    response = await self._post(
                        {"proposed_treatment": "Fictional proposed treatment"}
                    )

                self.assertEqual(response.status_code, 503, response.text)
                self.assertNotIn(sensitive_text, response.text)

    async def test_invalid_rpc_response_is_not_returned_to_patient(self):
        self._set_current_user(permissions=["submit_record_correction"])
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "request_id": 731,
                            "guardrail_decision": "Allow",
                            "approval_status": "Approved",
                        },
                    )
                ),
            ),
        ):
            response = await self._post(
                {"proposed_treatment": "Fictional proposed treatment"}
            )

        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn("Approved", response.text)
        self.assertIn("could not confirm", response.json()["detail"])


class PatientCorrectionRequestFailureHandlingTests(unittest.IsolatedAsyncioTestCase):
    """Exercises the real _request_supabase helper with a mocked HTTP client."""

    proposal = "Fictional proposed treatment (TEST ONLY)"

    async def asyncSetUp(self):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=42,
            email="user@example.test",
            role=Role.PATIENT,
            permissions=["submit_record_correction"],
        )
        self._had_client = hasattr(app.state, "supabase_client")
        self._previous_client = getattr(app.state, "supabase_client", None)
        self.http_client = Mock()
        self.http_client.request = AsyncMock()
        app.state.supabase_client = self.http_client
        self._env = patch.dict(
            "os.environ",
            {
                "SUPABASE_URL": "https://example.supabase.test",
                "SUPABASE_SERVICE_ROLE_KEY": "server-only-key",
            },
        )
        self._env.start()
        self._precheck = patch(
            "app.main._blocking_correction_request_id",
            new=AsyncMock(return_value=None),
        )
        self._precheck.start()

    async def asyncTearDown(self):
        self._precheck.stop()
        self._env.stop()
        app.dependency_overrides.pop(get_current_user, None)
        if self._had_client:
            app.state.supabase_client = self._previous_client
        else:
            del app.state.supabase_client

    async def _post(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.post(
                "/api/patient/correction-requests",
                json={"proposed_treatment": self.proposal},
            )

    def _assert_safe(self, response, logs):
        combined = response.text + "\n".join(logs.output)
        for secret in ("server-only-key", self.proposal, "Failing row contains"):
            self.assertNotIn(secret, combined)

    async def test_uncertain_transport_failures_return_unconfirmed_without_retry(self):
        rpc_request = httpx.Request(
            "POST",
            "https://example.supabase.test/rest/v1/rpc/submit_patient_treatment_correction",
        )
        for failure in (
            httpx.ReadTimeout("read timed out", request=rpc_request),
            httpx.WriteTimeout("write timed out", request=rpc_request),
            httpx.ReadError("connection reset", request=rpc_request),
            httpx.RemoteProtocolError("server disconnected", request=rpc_request),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.http_client.request.reset_mock()
                self.http_client.request.side_effect = failure
                with self.assertLogs("app.main", level="ERROR") as logs:
                    response = await self._post()

                self.assertEqual(response.status_code, 504, response.text)
                self.assertIn("could not confirm", response.json()["detail"])
                self.assertIn("check your request history", response.json()["detail"])
                self.http_client.request.assert_awaited_once()
                self.assertTrue(
                    any(type(failure).__name__ in line for line in logs.output)
                )
                self._assert_safe(response, logs)

    async def test_failures_before_the_request_is_sent_return_unavailable(self):
        rpc_request = httpx.Request(
            "POST",
            "https://example.supabase.test/rest/v1/rpc/submit_patient_treatment_correction",
        )
        for failure in (
            httpx.ConnectError("connection refused", request=rpc_request),
            httpx.ConnectTimeout("connect timed out", request=rpc_request),
            httpx.PoolTimeout("pool timed out", request=rpc_request),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.http_client.request.reset_mock()
                self.http_client.request.side_effect = failure
                with self.assertLogs("app.main", level="ERROR") as logs:
                    response = await self._post()

                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(
                    response.json()["detail"],
                    "Correction requests are temporarily unavailable.",
                )
                self.http_client.request.assert_awaited_once()
                self._assert_safe(response, logs)

    async def test_missing_service_role_key_does_not_call_supabase(self):
        with patch.dict("os.environ", {"SUPABASE_SERVICE_ROLE_KEY": ""}):
            with self.assertLogs("app.main", level="ERROR") as logs:
                response = await self._post()

        self.assertEqual(response.status_code, 503, response.text)
        self.http_client.request.assert_not_awaited()
        self._assert_safe(response, logs)

    async def test_database_errors_are_rolled_back_failures_without_logged_details(self):
        for status, code in ((409, "23505"), (400, "23502"), (400, "23514"), (400, "P0001")):
            with self.subTest(code=code):
                self.http_client.request.reset_mock()
                self.http_client.request.side_effect = None
                self.http_client.request.return_value = httpx.Response(
                    status,
                    json={
                        "code": code,
                        "message": f"constraint failed for {self.proposal}",
                        "details": f"Failing row contains (5, 42, 101, {self.proposal}).",
                        "hint": "server-only-key",
                    },
                )
                with self.assertLogs("app.main", level="ERROR") as logs:
                    response = await self._post()

                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(
                    response.json()["detail"],
                    "Correction requests are temporarily unavailable.",
                )
                self.assertTrue(any(code in line for line in logs.output))
                self.http_client.request.assert_awaited_once()
                self._assert_safe(response, logs)

    async def test_gateway_failures_without_database_code_are_unconfirmed(self):
        for status in (502, 504, 520, 524):
            with self.subTest(status=status):
                self.http_client.request.reset_mock()
                self.http_client.request.side_effect = None
                self.http_client.request.return_value = httpx.Response(
                    status,
                    text="<html>upstream timeout</html>",
                )
                with self.assertLogs("app.main", level="ERROR") as logs:
                    response = await self._post()

                self.assertEqual(response.status_code, 504, response.text)
                self.assertIn("could not confirm", response.json()["detail"])
                self._assert_safe(response, logs)

    async def test_postgrest_connection_errors_are_not_reported_as_unconfirmed(self):
        self.http_client.request.side_effect = None
        self.http_client.request.return_value = httpx.Response(
            503,
            json={"code": "PGRST001", "message": "Database client error."},
        )
        with self.assertLogs("app.main", level="ERROR") as logs:
            response = await self._post()

        self.assertEqual(response.status_code, 503, response.text)
        self._assert_safe(response, logs)


if __name__ == "__main__":
    unittest.main()
