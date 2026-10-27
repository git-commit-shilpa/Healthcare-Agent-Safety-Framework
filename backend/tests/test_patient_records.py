import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.main import CurrentUser, Role, app, get_current_user


class PatientMedicalRecordEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def _set_current_user(self, role, user_id=42):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id,
            email="user@example.test",
            role=role,
            permissions=[],
        )

    async def test_unauthenticated_request_is_rejected(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/patient/my-record")

        self.assertEqual(response.status_code, 401, response.text)

    async def test_authenticated_non_patient_roles_are_rejected_without_database_read(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for role in (Role.ADMIN, Role.PHYSICIAN, Role.TECHNICIAN):
                self._set_current_user(role)
                with self.subTest(role=role.value):
                    with patch("app.main._request_supabase", new=AsyncMock()) as request:
                        response = await client.get("/api/patient/my-record")

                    self.assertEqual(response.status_code, 403, response.text)
                    request.assert_not_awaited()

    async def test_patient_record_query_uses_authenticated_user_id_only(self):
        self._set_current_user(Role.PATIENT, user_id=42)
        supabase_response = httpx.Response(
            200,
            json=[
                {
                    "age": 34,
                    "medical_condition": "Fictional example condition",
                    "treatment": "Fictional example treatment",
                }
            ],
        )
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(return_value=supabase_response),
            ) as request,
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/patient/my-record?user_id=99&patient_id=99"
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "record": {
                    "age": 34,
                    "medical_condition": "Fictional example condition",
                    "treatment": "Fictional example treatment",
                }
            },
        )
        request.assert_awaited_once()
        self.assertEqual(
            request.await_args.args[0],
            "/rest/v1/patients",
        )
        self.assertEqual(
            request.await_args.kwargs["params"],
            {
                "select": "age,medical_condition,treatment",
                "user_id": "eq.42",
                "limit": "2",
            },
        )
        self.assertNotIn("patient_id", response.json()["record"])
        self.assertNotIn("user_id", response.json()["record"])

    async def test_patient_without_record_receives_null_record(self):
        self._set_current_user(Role.PATIENT)
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(return_value=httpx.Response(200, json=[])),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/patient/my-record")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"record": None})

    async def test_multiple_matching_records_fail_safely(self):
        self._set_current_user(Role.PATIENT)
        duplicate_records = [
            {"age": 34, "medical_condition": "Condition A", "treatment": "Treatment A"},
            {"age": 35, "medical_condition": "Condition B", "treatment": "Treatment B"},
        ]
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(
                    return_value=httpx.Response(200, json=duplicate_records)
                ),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/patient/my-record")

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            response.json()["detail"],
            "Multiple medical records are linked to this account.",
        )
        self.assertNotIn("Condition A", response.text)
        self.assertNotIn("Condition B", response.text)

    async def test_supabase_failure_returns_safe_error(self):
        self._set_current_user(Role.PATIENT)
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(
                    return_value=httpx.Response(
                        500,
                        text="sensitive database failure details",
                    )
                ),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/patient/my-record")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"],
            "Medical records are temporarily unavailable.",
        )
        self.assertNotIn("sensitive database failure details", response.text)

    async def test_supabase_request_exception_returns_safe_error(self):
        self._set_current_user(Role.PATIENT)
        with (
            patch("app.main._service_role_key", return_value="server-only-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(side_effect=httpx.HTTPError("sensitive request details")),
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.get("/api/patient/my-record")

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(
            response.json()["detail"],
            "Medical records are temporarily unavailable.",
        )
        self.assertNotIn("sensitive request details", response.text)


if __name__ == "__main__":
    unittest.main()
