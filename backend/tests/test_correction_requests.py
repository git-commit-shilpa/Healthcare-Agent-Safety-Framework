"""A Patient submits corrections and sees their history; nothing is written by the AI."""

import httpx
import logging
import unittest
from fastapi import HTTPException
from unittest.mock import AsyncMock, Mock, patch
from app import chat_state
from app.chat_state import issue_state
from app.intent_recognizer import Recognition
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
                "coordination": None,   # the database function predates 014 in this test
                "review_stage": "with_physician",
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


HISTORY_PATH = "/api/patient/correction-requests"


SENSITIVE_PROPOSAL = "Fictional proposed treatment (TEST ONLY)"


def _request_row(request_id, *, requested_by=42, decision="Escalate", created_at=None):
    return {
        "request_id": request_id,
        "requested_by": requested_by,
        "guardrail_decision": decision,
        "created_at": created_at or f"2026-10-30T19:4{request_id % 10}:00+00:00",
    }


class SupabaseRouter:
    """Routes mocked _request_supabase calls by table and records each call."""

    def __init__(self, *, requests=None, approvals=None, rpc=None, failures=None):
        self.responses = {
            "/rest/v1/ai_requests": requests if requests is not None else [],
            "/rest/v1/approvals": approvals if approvals is not None else [],
            "/rest/v1/technician_tasks": [],
            "/rest/v1/rpc/submit_patient_treatment_correction": rpc,
        }
        self.failures = failures or {}
        self.calls = []

    async def __call__(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if path in self.failures:
            failure = self.failures[path]
            if isinstance(failure, Exception):
                raise failure
            return failure
        response = self.responses[path]
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def calls_to(self, path):
        return [kwargs for called_path, kwargs in self.calls if called_path == path]


class PatientCorrectionHistoryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._key = patch("app.main._service_role_key", return_value="server-only-key")
        self._key.start()

    async def asyncTearDown(self):
        self._key.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def _set_current_user(self, role=Role.PATIENT, user_id=42, permissions=None):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id,
            email="user@example.test",
            role=role,
            permissions=permissions if permissions is not None else [],
        )

    async def _get(self, query=""):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get(HISTORY_PATH + query)

    async def _post(self, body=None):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.post(
                HISTORY_PATH,
                json=body or {"proposed_treatment": SENSITIVE_PROPOSAL},
            )


class CorrectionHistoryAuthorizationTests(PatientCorrectionHistoryTestCase):

    async def test_non_patient_roles_are_rejected_without_database_access(self):
        with patch("app.main._request_supabase", new=AsyncMock()) as request:
            for role in (Role.ADMIN, Role.PHYSICIAN, Role.TECHNICIAN):
                self._set_current_user(role, permissions=["view_own_correction_requests"])
                with self.subTest(role=role.value):
                    response = await self._get()
                    self.assertEqual(response.status_code, 403, response.text)

        request.assert_not_awaited()

    async def test_view_permission_is_required(self):
        self._set_current_user(permissions=["submit_record_correction"])
        with patch("app.main._request_supabase", new=AsyncMock()) as request:
            response = await self._get()

        self.assertEqual(response.status_code, 403, response.text)
        request.assert_not_awaited()


class CorrectionHistoryPrivacyTests(PatientCorrectionHistoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._set_current_user(permissions=["view_own_correction_requests"])

    async def test_query_uses_authenticated_user_and_ignores_client_identifiers(self):
        router = SupabaseRouter(
            requests=[_request_row(5)],
            approvals=[{"request_id": 5, "status": "Pending", "reviewed_at": None}],
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._get("?user_id=3&patient_id=102&requested_by=3")

        self.assertEqual(response.status_code, 200, response.text)
        request_params = router.calls_to("/rest/v1/ai_requests")[0]["params"]
        self.assertEqual(request_params["requested_by"], "eq.42")
        self.assertEqual(request_params["action"], "eq.patient_treatment_correction")
        self.assertNotIn("patient_id", request_params)
        approval_params = router.calls_to("/rest/v1/approvals")[0]["params"]
        self.assertEqual(approval_params["request_id"], "in.(5)")

    async def test_sensitive_columns_are_never_selected_or_returned(self):
        router = SupabaseRouter(
            requests=[
                {
                    **_request_row(5),
                    # Extra columns must not be passed through even if returned.
                    "request_details": f'{{"proposed_value":"{SENSITIVE_PROPOSAL}"}}',
                    "decision_reason": "Human review required.",
                    "patient_id": 101,
                }
            ],
            approvals=[
                {"request_id": 5, "status": "Pending", "reviewed_at": None, "reviewer_id": 4}
            ],
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._get()

        self.assertEqual(response.status_code, 200, response.text)
        request_select = router.calls_to("/rest/v1/ai_requests")[0]["params"]["select"]
        approval_select = router.calls_to("/rest/v1/approvals")[0]["params"]["select"]
        for column in ("request_details", "decision_reason", "patient_id", "action"):
            self.assertNotIn(column, request_select)
        self.assertNotIn("reviewer_id", approval_select)

        self.assertNotIn(SENSITIVE_PROPOSAL, response.text)
        item = response.json()["items"][0]
        self.assertEqual(
            set(item),
            {
                "request_id",
                "created_at",
                "guardrail_decision",
                "approval_status",
                "reviewed_at",
                "status_issue",
                "coordination",   # added by migration 014: null when no Technician task is linked
                "review_stage",   # added by migration 015
            },
        )

    async def test_rows_for_another_requester_fail_closed(self):
        router = SupabaseRouter(requests=[_request_row(9, requested_by=3)])
        with patch("app.main._request_supabase", new=router):
            response = await self._get()

        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn("9", response.json()["detail"])
        self.assertEqual(router.calls_to("/rest/v1/approvals"), [])

    async def test_unrelated_approval_rows_fail_closed(self):
        router = SupabaseRouter(
            requests=[_request_row(5)],
            approvals=[{"request_id": 6, "status": "Approved", "reviewed_at": "2026-10-31T00:00:00+00:00"}],
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._get()

        self.assertEqual(response.status_code, 502, response.text)

    async def test_supabase_failures_are_reported_without_details(self):
        failures = (
            httpx.Response(500, text="server-only-key details"),
            HTTPException(status_code=503, detail="server-only-key details"),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"unexpected": "object"}),
        )
        for failure in failures:
            with self.subTest(failure=repr(failure)[:40]):
                router = SupabaseRouter(failures={"/rest/v1/ai_requests": failure})
                with patch("app.main._request_supabase", new=router):
                    response = await self._get()

                self.assertIn(response.status_code, (502, 503), response.text)
                self.assertEqual(
                    response.json()["detail"],
                    "Your correction request history is temporarily unavailable.",
                )


class CorrectionHistoryStatusMappingTests(PatientCorrectionHistoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._set_current_user(permissions=["view_own_correction_requests"])

    async def _items(self, requests, approvals):
        router = SupabaseRouter(requests=requests, approvals=approvals)
        with patch("app.main._request_supabase", new=router):
            response = await self._get()
        self.assertEqual(response.status_code, 200, response.text)
        return {item["request_id"]: item for item in response.json()["items"]}

    async def test_consistent_statuses_are_returned(self):
        reviewed = "2026-10-31T10:00:00+00:00"
        items = await self._items(
            [_request_row(7), _request_row(6), _request_row(5)],
            [
                {"request_id": 5, "status": "Pending", "reviewed_at": None},
                {"request_id": 6, "status": "Approved", "reviewed_at": reviewed},
                {"request_id": 7, "status": "Rejected", "reviewed_at": reviewed},
            ],
        )

        self.assertEqual(list(items), [7, 6, 5])
        self.assertEqual(
            items[5],
            {
                "request_id": 5,
                "created_at": "2026-10-30T19:45:00+00:00",
                "guardrail_decision": "Escalate",
                "approval_status": "Pending",
                "reviewed_at": None,
                "status_issue": None,
                "coordination": None,
                "review_stage": "with_physician",
            },
        )
        self.assertEqual(items[6]["approval_status"], "Approved")
        self.assertEqual(items[6]["reviewed_at"], reviewed)
        self.assertEqual(items[7]["approval_status"], "Rejected")
        self.assertIsNone(items[7]["status_issue"])

    async def test_missing_approval_is_an_explicit_inconsistency_not_pending(self):
        with self.assertLogs("app.main", level="WARNING") as logs:
            items = await self._items([_request_row(5)], [])

        self.assertIsNone(items[5]["approval_status"])
        self.assertEqual(items[5]["status_issue"], "missing_approval")
        self.assertTrue(any("missing_approval" in line for line in logs.output))

    async def test_other_inconsistencies_are_flagged(self):
        reviewed = "2026-10-31T10:00:00+00:00"
        with self.assertLogs("app.main", level="WARNING"):
            items = await self._items(
                [
                    _request_row(1),
                    _request_row(2),
                    _request_row(3),
                    _request_row(4),
                    _request_row(5, decision="Maybe"),
                ],
                [
                    {"request_id": 1, "status": "Pending", "reviewed_at": None},
                    {"request_id": 1, "status": "Approved", "reviewed_at": reviewed},
                    {"request_id": 2, "status": "Unknown", "reviewed_at": None},
                    {"request_id": 3, "status": "Pending", "reviewed_at": reviewed},
                    {"request_id": 4, "status": "Approved", "reviewed_at": None},
                    {"request_id": 5, "status": "Pending", "reviewed_at": None},
                ],
            )

        self.assertEqual(items[1]["status_issue"], "multiple_approvals")
        self.assertIsNone(items[1]["approval_status"])
        self.assertEqual(items[2]["status_issue"], "invalid_approval_status")
        self.assertIsNone(items[2]["approval_status"])
        self.assertEqual(items[3]["status_issue"], "review_timestamp_mismatch")
        self.assertEqual(items[4]["status_issue"], "review_timestamp_mismatch")
        self.assertEqual(items[5]["status_issue"], "invalid_guardrail_decision")
        self.assertIsNone(items[5]["guardrail_decision"])

    async def test_allow_decision_does_not_imply_physician_approval(self):
        items = await self._items(
            [_request_row(5, decision="Allow")],
            [{"request_id": 5, "status": "Pending", "reviewed_at": None}],
        )
        self.assertEqual(items[5]["guardrail_decision"], "Allow")
        self.assertEqual(items[5]["approval_status"], "Pending")


class CorrectionSubmissionDuplicateTests(PatientCorrectionHistoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._set_current_user(permissions=["submit_record_correction"])

    def _rpc_success(self):
        return httpx.Response(
            200,
            json={"request_id": 8, "guardrail_decision": "Escalate", "approval_status": "Pending"},
        )

    async def test_pending_request_does_not_block_submission(self):
        # A patient may have any number of corrections open at once (demo requirement).
        router = SupabaseRouter(
            requests=[_request_row(5)],
            approvals=[{"request_id": 5, "status": "Pending", "reviewed_at": None}],
            rpc=self._rpc_success(),
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._post()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["request_id"], 8)
        self.assertEqual(
            len(router.calls_to("/rest/v1/rpc/submit_patient_treatment_correction")), 1
        )
        self.assertEqual(router.calls_to("/rest/v1/ai_requests"), [])  # no pre-check read

    async def test_submission_does_not_depend_on_reading_existing_requests(self):
        # There is no pre-check any more, so an unreadable request list cannot stop a submission.
        for failure in (
            httpx.Response(500, text="details"),
            HTTPException(status_code=503, detail="details"),
        ):
            with self.subTest(failure=type(failure).__name__):
                router = SupabaseRouter(
                    failures={"/rest/v1/ai_requests": failure},
                    rpc=self._rpc_success(),
                )
                with patch("app.main._request_supabase", new=router):
                    response = await self._post()

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    len(router.calls_to("/rest/v1/rpc/submit_patient_treatment_correction")), 1
                )

    async def test_precheck_runs_only_after_permission_and_input_validation(self):
        router = SupabaseRouter(rpc=self._rpc_success())
        with patch("app.main._request_supabase", new=router):
            self._set_current_user(permissions=[])
            forbidden = await self._post()
            self._set_current_user(permissions=["submit_record_correction"])
            invalid = await self._post({"proposed_treatment": "   "})

        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(router.calls, [])

    async def test_database_enforced_duplicate_error_maps_to_conflict(self):
        router = SupabaseRouter(
            requests=[],
            rpc=httpx.Response(
                409,
                json={
                    "code": "PT409",
                    "message": "A treatment correction request is already awaiting review.",
                    "details": SENSITIVE_PROPOSAL,
                },
            ),
        )
        with (
            patch("app.main._request_supabase", new=router),
            self.assertLogs("app.main", level="ERROR") as logs,
        ):
            response = await self._post()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("already awaiting review", response.json()["detail"])
        self.assertNotIn(SENSITIVE_PROPOSAL, response.text + "\n".join(logs.output))


ALL = ["request_ai_assistance", "view_own_record", "view_own_correction_requests", "submit_record_correction"]


PROPOSAL = "Fictional replacement schedule SECRET-PROPOSAL"


OK = {"request_id": 9, "guardrail_decision": "Escalate", "approval_status": "Pending"}


def user(uid=2, session="sess-A", permissions=None, role=Role.PATIENT):
    current = CurrentUser(user_id=uid, email="p@example.test", role=role, permissions=ALL if permissions is None else permissions)
    current._session_key = session
    return current


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, path, body, current=None, blocking=None, rpc=None):
        app.dependency_overrides[get_current_user] = lambda: current or user()
        rec = AsyncMock(return_value=Recognition('{"intent": "request_correction", "confidence": 0.9}', True))
        self.rpc = rpc or AsyncMock(return_value=httpx.Response(200, json=OK))
        self.rec = rec
        try:
            with patch("app.main._blocking_correction_request_id", AsyncMock(return_value=blocking)) as pending, \
                 patch("app.main.recognize_intent", rec), \
                 patch("app.main._write_chat_audit", AsyncMock()), \
                 patch("app.patient_chat.generate_reply", AsyncMock()) as gen, \
                 patch("app.main._service_role_key", return_value="svc"), \
                 patch("app.main._request_supabase", self.rpc):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post(path, json=body)
        finally:
            app.dependency_overrides.clear()
        self.pending, self.gen = pending, gen
        return response

    async def chat(self, message, token=None, **kw):
        body = {"message": message, **({"state_token": token} if token else {})}
        return await self.call("/api/patient/assistant/chat", body, **kw)

    async def to_details(self, **kw):
        offer = (await self.chat("correct my record")).json()["state_token"]
        return (await self.chat("yes", offer, **kw)).json()["state_token"]

    async def to_confirm(self, text=PROPOSAL):
        details = await self.to_details()
        response = await self.call("/api/patient/assistant/correction/review",
                                   {"proposed_treatment": text, "state_token": details})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def confirm(self, token, text=PROPOSAL, **kw):
        return await self.call("/api/patient/assistant/correction/confirm",
                               {"proposed_treatment": text, "state_token": token}, **kw)

    async def test_full_flow_submits_once_through_the_shared_rpc(self):
        review = await self.to_confirm()
        self.assertEqual(review["proposed_treatment"], PROPOSAL)
        self.assertIn("does not change your record", review["notice"])
        response = await self.confirm(review["state_token"])
        self.assertEqual((response.status_code, response.json()["request_id"]), (200, 9))
        self.rpc.assert_awaited_once()
        sent = self.rpc.await_args.kwargs
        self.assertIn("submit_patient_treatment_correction", self.rpc.await_args.args[0])
        self.assertEqual(sent["json_body"], {"p_requested_by": 2, "p_proposed_treatment": PROPOSAL})

    async def test_conversational_yes_and_review_never_submit_or_reach_a_model(self):
        details = await self.to_details()
        self.rpc.assert_not_awaited()
        review = await self.call("/api/patient/assistant/correction/review",
                                 {"proposed_treatment": PROPOSAL, "state_token": details})
        self.rpc.assert_not_awaited()
        self.gen.assert_not_awaited()
        reply = (await self.chat("yes", review.json()["state_token"])).json()
        self.assertEqual(reply["mode"], "notice")
        self.assertIn("press Confirm", reply["reply"])
        self.rpc.assert_not_awaited()

    async def test_chat_cancel_at_review_invalidates_confirm(self):
        review = await self.to_confirm()
        reply = (await self.chat("cancel", review["state_token"])).json()
        self.assertEqual(reply["mode"], "cancelled")
        self.assertEqual((await self.confirm(review["state_token"])).status_code, 409)
        self.rpc.assert_not_awaited()

    async def test_confirm_requires_the_exact_reviewed_text_and_a_confirm_token(self):
        review = await self.to_confirm()
        self.assertEqual((await self.confirm(review["state_token"], text=PROPOSAL + " edited")).status_code, 409)
        details = await self.to_details()
        self.assertEqual((await self.confirm(details)).status_code, 409)  # details token can't confirm
        offered = issue_state(2, "offered", session_key="sess-A")
        self.assertEqual((await self.confirm(offered)).status_code, 409)
        self.rpc.assert_not_awaited()

    async def test_replayed_confirm_does_not_submit_twice(self):
        review = await self.to_confirm()
        rpc = AsyncMock(return_value=httpx.Response(200, json=OK))
        self.assertEqual((await self.confirm(review["state_token"], rpc=rpc)).status_code, 200)
        self.assertEqual((await self.confirm(review["state_token"], rpc=rpc)).status_code, 409)
        rpc.assert_awaited_once()

    async def test_review_token_is_single_use_and_forged_or_expired_tokens_fail(self):
        details = await self.to_details()
        body = {"proposed_treatment": PROPOSAL, "state_token": details}
        self.assertEqual((await self.call("/api/patient/assistant/correction/review", body)).status_code, 200)
        self.assertEqual((await self.call("/api/patient/assistant/correction/review", body)).status_code, 409)
        for token in (details[:-3] + "AAA", "x.y",
                      issue_state(2, "details", now=1000, session_key="sess-A")):
            r = await self.call("/api/patient/assistant/correction/review", {**body, "state_token": token})
            self.assertEqual(r.status_code, 409)

    async def test_missing_login_session_fails_closed(self):
        details = issue_state(2, "details")
        r = await self.call("/api/patient/assistant/correction/review",
                            {"proposed_treatment": PROPOSAL, "state_token": details}, current=user(session=None))
        self.assertEqual(r.status_code, 409)

    async def test_roles_permissions_and_extra_fields_are_enforced(self):
        token = issue_state(2, "confirm", session_key="sess-A", proposal_hash=chat_state.bind_text(PROPOSAL))
        for current in (user(role=Role.ADMIN), user(role=Role.PHYSICIAN), user(role=Role.TECHNICIAN),
                        user(permissions=["request_ai_assistance"]), user(permissions=["submit_record_correction"])):
            self.assertEqual((await self.confirm(token, current=current)).status_code, 403)
        for extra in ({"patient_id": 101}, {"reviewer_id": 4}, {"user_id": 9}, {"decision": "Allow"}):
            r = await self.call("/api/patient/assistant/correction/confirm",
                                {"proposed_treatment": PROPOSAL, "state_token": token, **extra})
            self.assertEqual(r.status_code, 422)
        for text in ("", "   ", "x" * 2001):
            self.assertEqual((await self.confirm(token, text=text)).status_code, 422)
        self.rpc.assert_not_awaited()

    async def test_an_open_request_does_not_block_review_or_confirm(self):
        # A patient may have any number of corrections open at once (demo requirement).
        details = await self.to_details()
        r = await self.call("/api/patient/assistant/correction/review",
                            {"proposed_treatment": PROPOSAL, "state_token": details}, blocking=5)
        self.assertEqual(r.status_code, 200)
        review = await self.to_confirm()
        r = await self.confirm(review["state_token"], blocking=5)
        self.assertEqual((r.status_code, r.json()["request_id"]), (200, 9))
        self.rpc.assert_awaited_once()

    async def test_database_duplicate_guard_is_preserved(self):
        review = await self.to_confirm()
        rpc = AsyncMock(return_value=httpx.Response(409, json={"code": "PT409", "message": "pending"}))
        r = await self.confirm(review["state_token"], rpc=rpc)
        self.assertEqual(r.status_code, 409)
        self.assertIn("already awaiting review", r.json()["detail"])

    async def test_timeout_is_unconfirmed_not_retried_and_token_is_spent(self):
        review = await self.to_confirm()
        request = httpx.Request("POST", "http://svc/rpc")
        failure = HTTPException(status_code=503, detail="x")
        failure.__cause__ = httpx.ReadTimeout("slow", request=request)
        rpc = AsyncMock(side_effect=failure)
        r = await self.confirm(review["state_token"], rpc=rpc)
        self.assertEqual(r.status_code, 504)
        self.assertEqual(rpc.await_count, 1)
        self.assertEqual((await self.confirm(review["state_token"], rpc=rpc)).status_code, 409)
        self.assertEqual(rpc.await_count, 1)

    async def test_proposal_text_is_never_logged(self):
        with self.assertLogs(level=logging.DEBUG) as captured:
            review = await self.to_confirm()
            await self.confirm(review["state_token"])
            logging.getLogger("probe").debug("probe")
        self.assertNotIn("SECRET-PROPOSAL", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
