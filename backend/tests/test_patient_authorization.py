import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.main import (
    CurrentUser,
    Role,
    _load_linked_user,
    app,
    get_current_user,
)


class PatientAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    async def test_authenticated_patient_role_and_name_come_from_linked_account(self):
        with (
            patch(
                "app.main._verified_auth_user",
                new=AsyncMock(
                    return_value={
                        "auth_user_id": "auth-patient-id",
                        "email": "patient@example.test",
                    }
                ),
            ),
            patch(
                "app.main._load_linked_user",
                new=AsyncMock(return_value=(42, "Patient", "Alex Example")),
            ),
            patch(
                "app.main._load_permissions",
                new=AsyncMock(return_value={"patient": ["view_own_records"]}),
            ),
        ):
            user = await get_current_user(
                HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials="test-access-token",
                )
            )

        self.assertEqual(user.user_id, 42)
        self.assertEqual(user.role, Role.PATIENT)
        self.assertEqual(user.name, "Alex Example")
        self.assertEqual(user.permissions, ["view_own_records"])
        self.assertIsNone(user._admin_session_key)

    async def test_non_patient_authenticated_user_does_not_receive_account_name(self):
        with (
            patch(
                "app.main._verified_auth_user",
                new=AsyncMock(
                    return_value={
                        "auth_user_id": "auth-physician-id",
                        "email": "physician@example.test",
                    }
                ),
            ),
            patch(
                "app.main._load_linked_user",
                new=AsyncMock(return_value=(43, "Physician", "Physician Example")),
            ),
            patch(
                "app.main._load_permissions",
                new=AsyncMock(return_value={"physician": []}),
            ),
        ):
            user = await get_current_user(
                HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials="test-access-token",
                )
            )

        self.assertEqual(user.role, Role.PHYSICIAN)
        self.assertIsNone(user.name)

    async def test_linked_patient_name_is_read_from_existing_users_record(self):
        response = httpx.Response(
            200,
            json=[
                {
                    "user_id": 42,
                    "name": "  Alex Example  ",
                    "role": "Patient",
                    "is_active": True,
                }
            ],
        )
        with (
            patch("app.main._service_role_key", return_value="service-role-key"),
            patch(
                "app.main._request_supabase",
                new=AsyncMock(return_value=response),
            ) as request,
        ):
            linked_user = await _load_linked_user(
                {"auth_user_id": "auth-patient-id"}
            )

        self.assertEqual(linked_user, (42, "Patient", "Alex Example"))
        self.assertEqual(
            request.await_args.kwargs["params"]["select"],
            "user_id,name,role,is_active",
        )

    async def test_unlinked_and_inactive_accounts_are_rejected(self):
        account_responses = [
            httpx.Response(200, json=[]),
            httpx.Response(
                200,
                json=[
                    {
                        "user_id": 42,
                        "name": "Alex Example",
                        "role": "Patient",
                        "is_active": False,
                    }
                ],
            ),
        ]
        with patch("app.main._service_role_key", return_value="service-role-key"):
            for response in account_responses:
                with self.subTest(account=response.json()):
                    with patch(
                        "app.main._request_supabase",
                        new=AsyncMock(return_value=response),
                    ):
                        with self.assertRaises(HTTPException) as raised:
                            await _load_linked_user(
                                {"auth_user_id": "auth-patient-id"}
                            )

                    self.assertEqual(raised.exception.status_code, 403)


class PatientRoleAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def _authenticated_user_for_role(self, role, supplied_roles):
        async def get_authenticated_user():
            user = CurrentUser(
                user_id=42,
                email="user@example.test",
                role=role,
                permissions=["manage_users", "view_audit_logs"],
            )
            supplied_roles.append(user.role)
            return user

        return get_authenticated_user

    async def test_patient_can_access_only_patient_dashboard(self):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=42,
            email="patient@example.test",
            name="Alex Example",
            role=Role.PATIENT,
            permissions=["manage_users", "view_audit_logs"],
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            patient_dashboard = await client.get("/api/dashboard/patient")
            other_dashboards = {
                role: await client.get(f"/api/dashboard/{role}")
                for role in ("admin", "physician", "technician")
            }
            admin_endpoint = await client.get("/api/admin/user-management/access")

        self.assertEqual(patient_dashboard.status_code, 200, patient_dashboard.text)
        self.assertEqual(patient_dashboard.json()["role"], "patient")
        for role, response in other_dashboards.items():
            with self.subTest(role=role):
                self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(admin_endpoint.status_code, 403, admin_endpoint.text)
        self.assertEqual(admin_endpoint.json()["detail"], "Admin role is required.")

    async def test_non_patient_roles_cannot_access_patient_dashboard(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for role in (Role.ADMIN, Role.PHYSICIAN, Role.TECHNICIAN):
                supplied_roles = []
                app.dependency_overrides[get_current_user] = (
                    self._authenticated_user_for_role(role, supplied_roles)
                )
                with self.subTest(role=role.value):
                    response = await client.get("/api/dashboard/patient")
                    self.assertEqual(supplied_roles, [role])
                    self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":
    unittest.main()
