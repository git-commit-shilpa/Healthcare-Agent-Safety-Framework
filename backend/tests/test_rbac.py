"""Authentication and role-based access control for Admin, Patient and Technician accounts."""

import base64
import httpx
import json
import time
import unittest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from app.main import (
    CurrentUser,
    Role,
    _load_linked_user,
    app,
    get_current_user,
)
from app.main import (
    ADMIN_SESSION_IDLE_TIMEOUT_SECONDS,
    AdminSessionState,
    CurrentUser,
    Role,
    admin_sessions,
    app,
    get_current_user,
    _active_admin_session,
    _password_auth_timestamp,
    _verified_auth_user,
)
from app.main import CurrentUser, Role, app, get_current_user
from app.technician.permissions import TECHNICIAN_ACTIONS


def _admin_route_requests():
    request_bodies = {
        "/api/admin/users/invite": {
            "name": "Test User",
            "email": "test.user@example.test",
            "role": "patient",
        },
        "/api/admin/ai-requests/analyze": {
            "question": "Summarize blocked requests",
        },
        "/api/admin/audit-logs/analyze": {
            "question": "Summarize recent audit events",
        },
    }

    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/admin/"):
            continue

        path = route.path.replace("{target_user_id}", "999")
        for method in sorted(route.methods & {"GET", "POST", "PUT", "PATCH", "DELETE"}):
            yield method, path, request_bodies.get(route.path)


class AdminRoleAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    async def test_every_admin_endpoint_rejects_non_admin_with_admin_permissions(self):
        admin_requests = list(_admin_route_requests())
        self.assertGreater(len(admin_requests), 0)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for role in (Role.PATIENT, Role.PHYSICIAN, Role.TECHNICIAN):
                app.dependency_overrides[get_current_user] = lambda role=role: CurrentUser(
                    user_id=42,
                    email="user@example.test",
                    role=role,
                    permissions=[
                        "manage_users",
                        "monitor_ai_requests",
                        "view_audit_logs",
                    ],
                )
                for method, path, body in admin_requests:
                    with self.subTest(role=role.value, method=method, path=path):
                        response = await client.request(method, path, json=body)
                        self.assertEqual(response.status_code, 403, response.text)
                        self.assertEqual(response.json()["detail"], "Admin role is required.")

    async def test_every_admin_endpoint_rejects_unauthenticated_requests(self):
        admin_requests = list(_admin_route_requests())
        self.assertGreater(len(admin_requests), 0)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            for method, path, body in admin_requests:
                with self.subTest(method=method, path=path):
                    response = await client.request(method, path, json=body)
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(
                        response.json()["detail"],
                        "A valid Supabase access token is required.",
                    )

    async def test_admin_role_still_requires_the_endpoint_permission(self):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=42,
            email="admin@example.test",
            role=Role.ADMIN,
            permissions=[],
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/admin/user-management/access")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "You do not have permission to perform this action.",
        )

    async def test_admin_with_required_permission_can_access_admin_endpoint(self):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=42,
            email="admin@example.test",
            role=Role.ADMIN,
            permissions=["manage_users"],
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/admin/user-management/access")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"authorized": True})


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


class TechnicianAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def _set_user(self, role, permissions=()):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=7,
            email="tech@example.test",
            role=role,
            permissions=list(permissions),
        )

    def _client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    def _technician_endpoints(self):
        """(METHOD, concrete path) for every technician route.

        Read from the OpenAPI schema rather than ``app.routes``: newer FastAPI
        versions wrap routers added with ``include_router`` in an opaque object.
        """
        endpoints = []
        for path, operations in app.openapi()["paths"].items():
            if not path.startswith("/api/technician"):
                continue
            concrete = path.replace("{patient_id}", "1")
            for method in operations:
                endpoints.append((method.upper(), concrete))
        return endpoints

    async def test_unauthenticated_is_rejected(self):
        async with self._client() as client:
            response = await client.get("/api/technician/me")
        self.assertEqual(response.status_code, 401, response.text)

    async def test_technician_gets_own_account_and_only_technician_permissions(self):
        self._set_user(Role.TECHNICIAN, ["upload_test_result", "manage_users"])
        async with self._client() as client:
            response = await client.get("/api/technician/me")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["role"], "technician")
        self.assertEqual(body["permissions"], ["upload_test_result"])

    async def test_other_roles_are_rejected_on_every_technician_route(self):
        endpoints = self._technician_endpoints()
        self.assertIn(("GET", "/api/technician/me"), endpoints)
        self.assertIn(("GET", "/api/technician/patients/1"), endpoints)
        async with self._client() as client:
            for role in (Role.ADMIN, Role.PATIENT, Role.PHYSICIAN):
                # Grant every technician permission to prove role is what blocks.
                self._set_user(role, list(TECHNICIAN_ACTIONS))
                for method, path in endpoints:
                    with self.subTest(role=role.value, method=method, path=path):
                        with patch(
                            "app.main._request_supabase", new=AsyncMock()
                        ) as request:
                            response = await client.request(method, path)
                        self.assertEqual(response.status_code, 403, response.text)
                        request.assert_not_awaited()

    async def test_technician_does_not_gain_admin_or_patient_access(self):
        self._set_user(Role.TECHNICIAN, TECHNICIAN_ACTIONS)
        async with self._client() as client:
            for path in ("/api/admin/overview", "/api/patient/my-record"):
                with self.subTest(path=path):
                    with patch("app.main._request_supabase", new=AsyncMock()) as request:
                        response = await client.get(path)
                    self.assertEqual(response.status_code, 403, response.text)
                    request.assert_not_awaited()


def _encoded_jwt_payload(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8"))
    return f"header.{payload.decode('ascii').rstrip('=')}.signature"


def _admin_user(session_key="session-hash", password_auth_timestamp=1000):
    user = CurrentUser(
        user_id=42,
        email="admin@example.test",
        role=Role.ADMIN,
        permissions=["manage_users"],
    )
    user._admin_session_key = session_key
    user._password_auth_timestamp = password_auth_timestamp
    return user


class AdminIdleSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        admin_sessions.clear()

    def tearDown(self):
        admin_sessions.clear()
        app.dependency_overrides.pop(get_current_user, None)

    def test_password_auth_timestamp_ignores_refresh_and_invalid_amr_entries(self):
        self.assertEqual(
            _password_auth_timestamp(
                [
                    {"method": "token_refresh", "timestamp": 1100},
                    {"method": "password", "timestamp": 1050},
                    {"method": "password", "timestamp": True},
                    {"method": "password", "timestamp": float("inf")},
                ]
            ),
            1050,
        )
        self.assertIsNone(
            _password_auth_timestamp(
                [{"method": "token_refresh", "timestamp": 1100}]
            )
        )

    async def test_password_auth_claims_are_read_only_after_supabase_validates_token(self):
        auth_user_id = str(uuid4())
        session_id = str(uuid4())
        token = _encoded_jwt_payload(
            {
                "session_id": session_id,
                "amr": [
                    {"method": "password", "timestamp": 1050},
                    {"method": "token_refresh", "timestamp": 1100},
                ],
            }
        )
        response = httpx.Response(
            200,
            json={"id": auth_user_id, "email": "admin@example.test"},
        )

        with (
            patch("app.main._request_supabase", new=AsyncMock(return_value=response)) as request,
            patch.dict("os.environ", {"SUPABASE_KEY": "test-publishable-key"}),
        ):
            auth_user = await _verified_auth_user(token)

        request.assert_awaited_once()
        self.assertEqual(auth_user["session_id"], session_id)
        self.assertEqual(auth_user["password_auth_timestamp"], 1050)
        self.assertNotIn(token, repr(auth_user))

    def test_old_session_after_process_restart_fails_closed(self):
        user = _admin_user(password_auth_timestamp=999)
        with (
            patch("app.main.ADMIN_SESSION_PROCESS_STARTED_AT", 1000),
            patch("app.main.time.time", return_value=1001),
            patch("app.main.time.monotonic", return_value=50),
        ):
            with self.assertRaises(HTTPException) as raised:
                _active_admin_session(user)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Admin session expired. Sign in again.")
        self.assertNotIn("session-hash", admin_sessions)

    def test_fresh_password_login_initializes_session_and_idle_expiry_is_terminal(self):
        user = _admin_user(password_auth_timestamp=1000)
        with (
            patch("app.main.ADMIN_SESSION_PROCESS_STARTED_AT", 1000),
            patch("app.main.time.time", return_value=1000),
            patch("app.main.time.monotonic", return_value=50),
        ):
            session = _active_admin_session(user)

        self.assertEqual(session.last_activity, 50)

        with patch("app.main.time.time", return_value=1600), patch(
            "app.main.time.monotonic",
            return_value=50 + ADMIN_SESSION_IDLE_TIMEOUT_SECONDS,
        ):
            with self.assertRaises(HTTPException) as raised:
                _active_admin_session(user)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertTrue(session.expired)

        with patch("app.main.time.time", return_value=1601), patch(
            "app.main.time.monotonic", return_value=51
        ):
            with self.assertRaises(HTTPException):
                _active_admin_session(user)
        self.assertTrue(session.expired)

    async def test_activity_endpoint_refreshes_active_session_and_logout_expires_it(self):
        user = _admin_user()
        session = AdminSessionState(last_activity=time.monotonic())
        previous_activity = session.last_activity
        admin_sessions["session-hash"] = session
        app.dependency_overrides[get_current_user] = lambda: user

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            activity = await client.post("/api/admin/session/activity")
            logout = await client.post("/api/admin/session/logout")
            after_logout = await client.post("/api/admin/session/activity")

        self.assertEqual(activity.status_code, 200, activity.text)
        self.assertGreaterEqual(session.last_activity, previous_activity)
        self.assertEqual(logout.status_code, 200, logout.text)
        self.assertTrue(session.expired)
        self.assertEqual(after_logout.status_code, 401, after_logout.text)

    async def test_expired_session_is_rejected_by_admin_endpoint(self):
        user = _admin_user()
        admin_sessions["session-hash"] = AdminSessionState(
            last_activity=100,
            expired=True,
        )

        async def current_user_with_session_check():
            return _active_admin_session(user) and user

        app.dependency_overrides[get_current_user] = current_user_with_session_check

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/admin/user-management/access")

        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(
            response.json()["detail"],
            "Admin session expired. Sign in again.",
        )


if __name__ == "__main__":
    unittest.main()
