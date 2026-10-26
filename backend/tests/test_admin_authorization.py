import unittest

import httpx
from fastapi.routing import APIRoute

from app.main import CurrentUser, Role, app, get_current_user


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


if __name__ == "__main__":
    unittest.main()
