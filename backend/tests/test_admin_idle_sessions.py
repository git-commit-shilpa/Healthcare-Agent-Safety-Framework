import base64
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import HTTPException

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
