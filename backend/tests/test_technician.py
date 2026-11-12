"""Technician tasks, assigned patients and decisions."""

import httpx
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from app.main import CurrentUser, Role, app, get_current_user
from app.technician import catalogue, gateway
from app.technician.errors import TaskApiError
from app.technician.features import service_requests_enabled
from app.technician.rpc import call_rpc, map_rpc_error
from tests.fake_supabase import FakeSupabase


DATABASE_SETUP = (Path(__file__).resolve().parents[2] / "database" / "database_setup.sql")


class FeatureFlagTests(unittest.TestCase):
    def test_off_unless_exactly_true(self):
        for value, expected in (("true", True), (" true ", True), ("TRUE", False), ("1", False),
                                ("yes", False), ("false", False), ("", False)):
            with self.subTest(value=value), patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": value}):
                self.assertIs(service_requests_enabled(), expected)

    def test_off_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HASF_SERVICE_REQUESTS_ENABLED", None)
            self.assertFalse(service_requests_enabled())


class CatalogueMatchesMigrationTests(unittest.TestCase):
    def test_categories_match_the_check_constraint_and_clinical_flag(self):
        sql = DATABASE_SETUP.read_text(encoding="utf-8")
        block = re.search(r"technician_tasks_category_check check \(category in \((.*?)\)\)", sql, re.S).group(1)
        in_sql = set(re.findall(r"'([a-z_]+)'", block))
        self.assertEqual(in_sql, set(catalogue.ALL_CATEGORIES))
        clinical = re.search(r"category not in \((.*?)\)", sql, re.S).group(1)
        self.assertEqual(set(re.findall(r"'([a-z_]+)'", clinical)), set(catalogue.CLINICAL_CATEGORIES))
        rpc = DATABASE_SETUP.read_text(encoding="utf-8")
        self.assertIn("'medication_request', 'treatment_change_request', 'diagnosis_inquiry'", rpc)

    def test_statuses_match_the_check_constraint(self):
        sql = DATABASE_SETUP.read_text(encoding="utf-8")
        block = re.search(r"technician_tasks_status_check check \(status in \((.*?)\)\)", sql, re.S).group(1)
        self.assertEqual(set(re.findall(r"'([A-Za-z ]+)'", block)), set(catalogue.ALL_STATUSES))


def verdict(**overrides):
    base = dict(permissions=["update_task_status"], decision="ALLOW", hold_reason=None,
                category="general_inquiry", requires_physician_approval=False,
                approval_status="none", current_status="In Review")
    base.update(overrides)
    return gateway.evaluate(**base)


class GatewayTests(unittest.TestCase):
    def test_missing_permission_blocks_everything(self):
        for decision in ("ALLOW", "BLOCK", "HOLD"):
            self.assertEqual(verdict(permissions=[], decision=decision, hold_reason="needs_information").decision, "Block")

    def test_routine_allow_is_allowed(self):
        self.assertEqual(verdict().decision, "Allow")

    def test_clinical_allow_never_allowed_without_a_valid_approval(self):
        for category in catalogue.CLINICAL_CATEGORIES:
            for status, expected in (("none", "Escalate"), ("pending", "Escalate"), ("rejected", "Block"),
                                     ("invalid", "Block"), ("approved", "Allow")):
                with self.subTest(category=category, status=status):
                    r = verdict(category=category, requires_physician_approval=True, approval_status=status)
                    self.assertEqual(r.decision, expected)

    def test_flagged_or_physician_hold_tasks_need_approval_even_if_routine(self):
        self.assertEqual(verdict(requires_physician_approval=True).decision, "Escalate")
        self.assertEqual(verdict(current_status="Awaiting Physician", approval_status="pending").decision, "Escalate")
        self.assertEqual(verdict(current_status="Awaiting Physician", approval_status="approved").decision, "Allow")

    def test_block_and_holds(self):
        self.assertEqual(verdict(decision="BLOCK").decision, "Allow")
        self.assertEqual(verdict(decision="HOLD", hold_reason="needs_information").decision, "Allow")
        self.assertEqual(verdict(decision="HOLD", hold_reason="physician_approval").decision, "Escalate")

    def test_unknown_inputs_fail_closed(self):
        self.assertEqual(verdict(decision="APPROVE").decision, "Block")
        self.assertEqual(verdict(decision="HOLD", hold_reason=None).decision, "Block")
        self.assertEqual(verdict(decision="HOLD", hold_reason="because").decision, "Block")


class RpcTests(unittest.IsolatedAsyncioTestCase):
    def _patch(self, response=None, side_effect=None):
        return (
            patch("app.main._service_role_key", return_value="k"),
            patch("app.main._request_supabase", new=AsyncMock(return_value=response, side_effect=side_effect)),
        )

    async def test_success_returns_the_json_object_and_sends_all_parameters_to_the_rpc_path(self):
        key, req = self._patch(httpx.Response(200, json={"task_id": 5, "status": "In Review"}))
        with key, req as request:
            result = await call_rpc("start_technician_task_review", {"p_task_id": 5, "p_actor_user_id": 6})
        self.assertEqual(result, {"task_id": 5, "status": "In Review"})
        args, kwargs = request.call_args
        self.assertEqual(args[0], "/rest/v1/rpc/start_technician_task_review")
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["json_body"], {"p_task_id": 5, "p_actor_user_id": 6})

    async def test_database_errors_map_to_safe_messages_without_leaking_database_text(self):
        cases = [
            ("PT404", "not_found", 404), ("PT409", "invalid_state", 409), ("PT409", "approval_required", 409),
            ("PT409", "no_unique_reviewer", 409), ("PT422", "no_eligible_technician", 422),
            ("PT429", "too_many_open", 429), ("PT403", "permission_missing", 403),
            ("PT422", "something_new", 422), ("42501", None, 403),
        ]
        for code, reason, status in cases:
            with self.subTest(code=code, reason=reason):
                body = {"code": code, "message": "SECRET internal text", "details": reason, "hint": "SECRET hint"}
                error = map_rpc_error(httpx.Response(400, json=body))
                self.assertEqual(error.status_code, status)
                self.assertNotIn("SECRET", error.detail)
                self.assertNotIn("SECRET", error.code)

    async def test_unknown_database_error_is_503_and_unconfirmed_gateway_is_504(self):
        self.assertEqual(map_rpc_error(httpx.Response(400, json={"code": "XX000", "message": "boom"})).status_code, 503)
        self.assertEqual(map_rpc_error(httpx.Response(504, text="gateway")).status_code, 504)
        self.assertEqual(map_rpc_error(httpx.Response(500, text="not json")).status_code, 503)

    async def test_transport_failures_distinguish_not_sent_from_unconfirmed(self):
        for exc, status in ((httpx.ConnectError("x"), 503), (httpx.ReadTimeout("x"), 504)):
            from fastapi import HTTPException

            key, req = self._patch(side_effect=HTTPException(status_code=503, detail="x"))
            wrapped = HTTPException(status_code=503, detail="x")
            wrapped.__cause__ = exc
            key, req = self._patch(side_effect=wrapped)
            with key, req, self.subTest(exc=type(exc).__name__):
                with self.assertRaises(TaskApiError) as ctx:
                    await call_rpc("complete_technician_task", {})
                self.assertEqual(ctx.exception.status_code, status)


class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_api_error_renders_detail_and_code_only(self):
        from app.technician.errors import register_error_handlers  # already registered on app

        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=6, email="t@example.test", role=Role.TECHNICIAN, permissions=["view_assigned_task"])
        try:
            with patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "false"}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                    response = await c.get("/api/technician/tasks")
        finally:
            app.dependency_overrides.pop(get_current_user, None)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json(), {"detail": "This feature is not available yet.", "code": "feature_disabled"})


PERM = ["view_assigned_patient"]


PATIENTS = {
    101: {"patient_id": 101, "user_id": 2, "age": 40, "medical_condition": "Fictional A", "treatment": "Fictional tx A"},
    102: {"patient_id": 102, "user_id": 3, "age": 50, "medical_condition": "Fictional B", "treatment": "Fictional tx B"},
}


def fake_supabase(assigned):
    """Dispatch on table; honours the eq./in. filters the service sends."""
    calls = []

    async def handler(path, **kw):
        calls.append((kw.get("method", "GET"), path, kw.get("params"), kw.get("json_body")))
        params = kw.get("params") or {}
        if kw.get("method") == "POST":
            return httpx.Response(201)
        if path.endswith("/patient_assignments"):
            rows = [{"patient_id": p} for p in assigned]
            if "patient_id" in params:
                want = int(params["patient_id"].removeprefix("eq."))
                rows = [r for r in rows if r["patient_id"] == want]
            return httpx.Response(200, json=rows)
        if path.endswith("/patients"):
            f = params["patient_id"]
            if f.startswith("in."):
                ids = [int(x) for x in f[4:-1].split(",")]
            else:
                ids = [int(f.removeprefix("eq."))]
            return httpx.Response(200, json=[PATIENTS[i] for i in ids if i in PATIENTS])
        if path.endswith("/users"):
            f = params["user_id"]
            ids = [int(x) for x in f[4:-1].split(",")]
            return httpx.Response(200, json=[{"user_id": i, "name": f"Fictional Patient {i}"} for i in ids])
        return httpx.Response(404, json=[])

    return handler, calls


class TechnicianPatientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_current_user, None)

    def _user(self, role=Role.TECHNICIAN, perms=PERM, user_id=6):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id, email="t@example.test", role=role, permissions=list(perms)
        )

    def _client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def _get(self, path, assigned=(101,)):
        handler, calls = fake_supabase(assigned)
        with (
            patch("app.main._service_role_key", return_value="k"),
            patch("app.main._request_supabase", new=AsyncMock(side_effect=handler)),
        ):
            async with self._client() as c:
                return await c.get(path), calls

    async def test_list_returns_only_assigned_patients(self):
        self._user()
        r, calls = await self._get("/api/technician/patients", assigned=[101])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([p["patient_id"] for p in r.json()], [101])
        self.assertEqual([p["name"] for p in r.json()], ["Fictional Patient 2"])
        users_call = next(c for c in calls if c[1].endswith("/users"))
        self.assertEqual(users_call[2]["user_id"], "in.(2)")  # only the assigned patient's user
        assignment = next(c for c in calls if c[1].endswith("/patient_assignments"))
        self.assertEqual(assignment[2]["staff_user_id"], "eq.6")

    async def test_assigned_detail_is_readable_and_audited(self):
        self._user()
        r, calls = await self._get("/api/technician/patients/101")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            set(r.json()), {"patient_id", "name", "age", "medical_condition", "treatment"}
        )
        self.assertEqual(r.json()["name"], "Fictional Patient 2")
        self.assertNotIn("user_id", r.json())
        audit = [c for c in calls if c[0] == "POST"]
        self.assertEqual(audit[0][3]["event_type"], "technician.patient.viewed")
        self.assertEqual(audit[0][3]["user_id"], 6)

    async def test_unassigned_patient_is_404_audited_and_not_read(self):
        self._user()
        r, calls = await self._get("/api/technician/patients/102", assigned=[101])
        self.assertEqual(r.status_code, 404, r.text)
        self.assertFalse(any(c[1].endswith("/patients") for c in calls))
        # Names are looked up only after the assignment check passes.
        self.assertFalse(any(c[1].endswith("/users") for c in calls))
        audit = [c for c in calls if c[0] == "POST"]
        self.assertEqual(audit[0][3]["event_type"], "technician.patient.access_denied")
        self.assertEqual(json.loads(audit[0][3]["details"]), {"patient_id": 102})

    async def test_unknown_patient_looks_identical_to_unassigned(self):
        self._user()
        r1, _ = await self._get("/api/technician/patients/102", assigned=[101])
        r2, _ = await self._get("/api/technician/patients/999", assigned=[101])
        self.assertEqual((r1.status_code, r1.json()), (r2.status_code, r2.json()))

    async def test_missing_permission_is_403_without_database_read(self):
        self._user(perms=[])
        with patch("app.main._request_supabase", new=AsyncMock()) as req:
            async with self._client() as c:
                for path in ("/api/technician/patients", "/api/technician/patients/101"):
                    self.assertEqual((await c.get(path)).status_code, 403)
        req.assert_not_awaited()

    async def test_other_roles_are_403_even_with_the_permission(self):
        for role in (Role.ADMIN, Role.PATIENT, Role.PHYSICIAN):
            self._user(role=role)
            with patch("app.main._request_supabase", new=AsyncMock()) as req:
                async with self._client() as c:
                    for path in ("/api/technician/patients", "/api/technician/patients/101"):
                        with self.subTest(role=role.value, path=path):
                            self.assertEqual((await c.get(path)).status_code, 403)
            req.assert_not_awaited()

    async def test_audit_failure_does_not_break_the_read(self):
        self._user()
        handler, _ = fake_supabase([101])

        async def flaky(path, **kw):
            if kw.get("method") == "POST":
                raise RuntimeError("audit down")
            return await handler(path, **kw)

        with (
            patch("app.main._service_role_key", return_value="k"),
            patch("app.main._request_supabase", new=AsyncMock(side_effect=flaky)),
        ):
            async with self._client() as c:
                r = await c.get("/api/technician/patients/101")
        self.assertEqual(r.status_code, 200, r.text)


PERMS = ["view_assigned_task", "update_task_status", "view_assigned_patient"]


def task(task_id, *, patient_id=101, requested_by=2, assigned_to=6, status="Submitted", category="general_inquiry",
         approval_request_id=None, requires=False, **extra):
    return {"task_id": task_id, "patient_id": patient_id, "requested_by": requested_by, "assigned_to": assigned_to,
            "category": category, "title": f"Title {task_id}", "description": f"Description {task_id}",
            "status": status, "latest_decision": None, "patient_message": None,
            "requires_physician_approval": requires, "approval_request_id": approval_request_id,
            "created_at": f"2026-01-0{task_id}T00:00:00Z", "updated_at": f"2026-01-0{task_id}T01:00:00Z", **extra}


def make_db(**overrides):
    tables = dict(
        patient_assignments=[{"patient_id": 101, "staff_user_id": 6}, {"patient_id": 102, "staff_user_id": 6},
                             {"patient_id": 101, "staff_user_id": 7}],
        users=[{"user_id": 2, "name": "Fictional Patient A"}, {"user_id": 3, "name": "Fictional Patient B"}],
        technician_tasks=[
            task(1), task(2, status="In Progress"), task(3, patient_id=102, requested_by=3, status="Completed"),
            task(4, assigned_to=7, patient_id=101),                      # another technician's task
            task(5, patient_id=103, requested_by=9),                     # patient the technician is NOT assigned to
            task(6, status="Awaiting Physician", category="medication_request", requires=True, approval_request_id=50),
        ],
        technician_task_events=[
            {"event_id": 1, "task_id": 1, "actor_user_id": 2, "actor_role": "Patient", "event_type": "created",
             "to_status": "Submitted", "internal_note": None, "created_at": "2026-01-01T00:00:00Z"},
            {"event_id": 2, "task_id": 1, "actor_user_id": 6, "actor_role": "Technician", "event_type": "decision",
             "decision": "HOLD", "hold_reason": "needs_information", "internal_note": "INTERNAL NOTE",
             "patient_message": "Please send dates.", "created_at": "2026-01-01T02:00:00Z"},
        ],
        approvals=[{"request_id": 50, "status": "Pending"}],
    )
    tables.update(overrides)
    return FakeSupabase(**tables)


class TaskReadBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env = patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "true"})
        self._env.start()

    async def asyncTearDown(self):
        self._env.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def user(self, role=Role.TECHNICIAN, perms=PERMS, user_id=6):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id, email="t@example.test", role=role, permissions=list(perms))

    async def get(self, path, db=None):
        db = db or make_db()
        with patch("app.main._service_role_key", return_value="k"), \
                patch("app.main._request_supabase", new=db.request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.get(path), db


class TaskListTests(TaskReadBase):
    async def test_lists_only_own_tasks_for_assigned_patients_with_names(self):
        self.user()
        r, db = await self.get("/api/technician/tasks")
        self.assertEqual(r.status_code, 200, r.text)
        ids = sorted(i["task_id"] for i in r.json()["items"])
        self.assertEqual(ids, [1, 2, 3, 6])  # not 4 (other technician) and not 5 (unassigned patient)
        item = next(i for i in r.json()["items"] if i["task_id"] == 1)
        self.assertEqual(item["patient"], {"patient_id": 101, "name": "Fictional Patient A"})
        # No notes, description or approval ids in list items.
        self.assertEqual(set(item) & {"description", "internal_note", "approval_request_id", "events"}, set())
        scoped = next(c for c in db.calls if c[1].endswith("/technician_tasks"))
        self.assertEqual(scoped[2]["assigned_to"], "eq.6")
        self.assertEqual(scoped[2]["patient_id"], "in.(101,102)")

    async def test_status_filter_and_pagination_and_bad_filter(self):
        self.user()
        r, _ = await self.get("/api/technician/tasks?status=In%20Progress")
        self.assertEqual([i["task_id"] for i in r.json()["items"]], [2])
        r, _ = await self.get("/api/technician/tasks?limit=1&offset=1")
        self.assertEqual(len(r.json()["items"]), 1)
        r, _ = await self.get("/api/technician/tasks?status=ALLOW")
        self.assertEqual((r.status_code, r.json()["code"]), (422, "invalid_request"))
        r, _ = await self.get("/api/technician/tasks?limit=1000")
        self.assertEqual(r.status_code, 422)

    async def test_approval_status_comes_from_the_approvals_table(self):
        self.user()
        r, _ = await self.get("/api/technician/tasks")
        by_id = {i["task_id"]: i for i in r.json()["items"]}
        self.assertEqual(by_id[6]["approval_status"], "pending")
        self.assertEqual(by_id[1]["approval_status"], "none")
        db = make_db(approvals=[{"request_id": 50, "status": "Approved"}])
        r, _ = await self.get("/api/technician/tasks", db)
        self.assertEqual({i["task_id"]: i["approval_status"] for i in r.json()["items"]}[6], "approved")
        db = make_db(approvals=[{"request_id": 50, "status": "Approved"}, {"request_id": 50, "status": "Approved"}])
        r, _ = await self.get("/api/technician/tasks", db)
        self.assertEqual({i["task_id"]: i["approval_status"] for i in r.json()["items"]}[6], "invalid")

    async def test_technician_without_assignments_sees_nothing_and_no_task_query_runs(self):
        self.user()
        db = make_db(patient_assignments=[])
        r, db = await self.get("/api/technician/tasks", db)
        self.assertEqual(r.json()["items"], [])
        self.assertNotIn("technician_tasks", db.read_tables())


class TaskDetailTests(TaskReadBase):
    async def test_detail_includes_history_with_internal_note_for_the_assigned_technician(self):
        self.user()
        r, db = await self.get("/api/technician/tasks/1")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["description"], "Description 1")
        notes = [e["internal_note"] for e in body["events"]]
        self.assertEqual(notes, [None, "INTERNAL NOTE"])
        self.assertEqual([e["by_me"] for e in body["events"]], [False, True])
        self.assertNotIn("actor_user_id", body["events"][0])
        self.assertEqual(db.audits()[0]["event_type"], "technician.task.viewed")

    async def test_other_technicians_unassigned_and_unknown_tasks_are_identical_404s(self):
        self.user()
        results = []
        for task_id in (4, 5, 999):
            r, db = await self.get(f"/api/technician/tasks/{task_id}")
            results.append((r.status_code, r.json()))
            self.assertEqual(db.audits()[0]["event_type"], "technician.task.access_denied")
            self.assertNotIn("technician_task_events", db.read_tables())
        self.assertEqual(len({json.dumps(x, sort_keys=True) for x in results}), 1)
        self.assertEqual(results[0], (404, {"detail": "Task not found.", "code": "not_found"}))

    async def test_a_technician_removed_from_the_patient_loses_access(self):
        self.user()
        db = make_db(patient_assignments=[{"patient_id": 102, "staff_user_id": 6}])
        r, _ = await self.get("/api/technician/tasks/1", db)
        self.assertEqual(r.status_code, 404)


class DashboardAndApprovalTests(TaskReadBase):
    async def test_dashboard_counts(self):
        self.user()
        r, _ = await self.get("/api/technician/dashboard")
        body = r.json()
        self.assertEqual((body["assigned_patients"], body["open_tasks"], body["pending_review"], body["in_progress"],
                          body["awaiting_physician"], body["completed"]), (2, 3, 1, 1, 1, 1))
        self.assertLessEqual(len(body["recent_tasks"]), 5)

    async def test_approvals_list_shows_only_my_escalated_tasks_without_reviewer_identity(self):
        self.user()
        r, _ = await self.get("/api/technician/approvals")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([(a["task_id"], a["approval_status"]) for a in r.json()], [(6, "pending")])
        self.assertEqual(set(r.json()[0]), {"task_id", "patient", "category", "task_status", "approval_status"})


class AccessAndGateTests(TaskReadBase):
    PATHS = ["/api/technician/dashboard", "/api/technician/tasks", "/api/technician/tasks/1", "/api/technician/approvals"]

    async def test_unauthenticated_is_401(self):
        db = make_db()
        with patch("app.main._request_supabase", new=db.request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                for path in self.PATHS:
                    self.assertEqual((await c.get(path)).status_code, 401, path)
        self.assertEqual(db.calls, [])

    async def test_other_roles_are_403_without_any_database_read(self):
        for role in (Role.ADMIN, Role.PATIENT, Role.PHYSICIAN):
            self.user(role=role)
            for path in self.PATHS:
                with self.subTest(role=role.value, path=path):
                    r, db = await self.get(path)
                    self.assertEqual(r.status_code, 403, r.text)
                    self.assertEqual(db.calls, [])

    async def test_technician_without_the_permission_is_403(self):
        self.user(perms=["view_assigned_patient"])
        for path in self.PATHS:
            r, db = await self.get(path)
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(db.calls, [])

    async def test_feature_flag_off_returns_503_before_any_database_read(self):
        self.user()
        for value in ("false", "", "TRUE"):
            with patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": value}):
                for path in self.PATHS:
                    with self.subTest(flag=value, path=path):
                        r, db = await self.get(path)
                        self.assertEqual(r.status_code, 503, r.text)
                        self.assertEqual(r.json()["code"], "feature_disabled")
                        self.assertEqual(db.calls, [])


def rpc_ok(outcome="executed", status="In Progress", decision="ALLOW", approval=None):
    return lambda payload: {"task_id": payload["p_task_id"], "status": status, "latest_decision": decision,
                            "outcome": outcome, "approval_request_id": None, "approval_status": approval}


class WriteBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env = patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "true"})
        self._env.start()

    async def asyncTearDown(self):
        self._env.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def user(self, role=Role.TECHNICIAN, perms=PERMS, user_id=6):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id, email="t@example.test", role=role, permissions=list(perms))

    async def post(self, path, body=None, db=None, rpc=None):
        db = db or make_db()
        for name in ("start_technician_task_review", "record_technician_task_decision", "complete_technician_task"):
            db.rpc.setdefault(name, rpc or rpc_ok())
        with patch("app.main._service_role_key", return_value="k"), \
                patch("app.main._request_supabase", new=db.request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.post(path, json=body), db


def db_with(**task_fields):
    base = task(1, status="In Review")
    base.update(task_fields)
    others = [t for t in make_db().tables["technician_tasks"] if t["task_id"] != 1]
    return make_db(technician_tasks=[base, *others])


class StartReviewTests(WriteBase):
    async def test_submitted_and_on_hold_tasks_can_start_review(self):
        self.user()
        for status in ("Submitted", "On Hold"):
            r, db = await self.post("/api/technician/tasks/1/start-review", db=db_with(status=status),
                                    rpc=rpc_ok(status="In Review", decision=None))
            self.assertEqual((r.status_code, r.json()["status"]), (200, "In Review"), r.text)
            self.assertEqual(db.rpc_calls[0], ("start_technician_task_review", {"p_task_id": 1, "p_actor_user_id": 6}))

    async def test_wrong_state_is_409_and_calls_no_rpc(self):
        self.user()
        for status in ("In Review", "In Progress", "Completed", "Declined", "Awaiting Physician"):
            r, db = await self.post("/api/technician/tasks/1/start-review", db=db_with(status=status))
            self.assertEqual((r.status_code, r.json()["code"]), (409, "invalid_state"))
            self.assertEqual(db.rpc_calls, [])

    async def test_other_technicians_and_unassigned_tasks_are_404_and_call_no_rpc(self):
        self.user()
        for task_id in (4, 5, 999):
            r, db = await self.post(f"/api/technician/tasks/{task_id}/start-review")
            self.assertEqual(r.status_code, 404)
            self.assertEqual(db.rpc_calls, [])


class DecisionTests(WriteBase):
    URL = "/api/technician/tasks/1/decision"

    async def test_allow_on_routine_task_sends_an_allow_verdict_and_the_verified_actor(self):
        self.user()
        r, db = await self.post(self.URL, {"decision": "ALLOW"}, db=db_with())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["outcome"], body["guardrail_decision"], body["guardrail_reason"]),
                         ("executed", "Allow", "routine_permitted"))
        name, payload = db.rpc_calls[0]
        self.assertEqual(name, "record_technician_task_decision")
        self.assertEqual(payload["p_actor_user_id"], 6)  # from the verified token, never the body
        self.assertEqual((payload["p_guardrail_decision"], payload["p_decision"]), ("Allow", "ALLOW"))
        self.assertTrue(payload["p_patient_message"])    # standard message supplied by the backend
        self.assertEqual(set(payload), {"p_task_id", "p_actor_user_id", "p_decision", "p_hold_reason", "p_internal_note",
                                        "p_patient_message", "p_guardrail_decision", "p_guardrail_reason"})

    async def test_backend_overrides_a_technician_allow_on_a_clinical_task_with_no_approval(self):
        self.user()
        db = db_with(category="medication_request", requires_physician_approval=True)
        r, db = await self.post(self.URL, {"decision": "ALLOW", "internal_note": "refill"}, db=db,
                                rpc=rpc_ok(outcome="escalated", status="In Review", decision=None))
        body = r.json()
        self.assertEqual((r.status_code, body["outcome"], body["guardrail_decision"]), (200, "escalated", "Escalate"))
        self.assertEqual(body["status"], "In Review")
        payload = db.rpc_calls[0][1]
        self.assertEqual(payload["p_guardrail_decision"], "Escalate")
        self.assertIsNone(payload["p_patient_message"])  # nothing patient-facing is stored for a rejected allow

    async def test_pending_rejected_and_invalid_approvals_never_yield_an_allow_verdict(self):
        self.user()
        for state, expected in (("Pending", "Escalate"), ("Rejected", "Block")):
            db = make_db(technician_tasks=[task(1, status="Awaiting Physician", category="medication_request",
                                                requires=True, approval_request_id=50), *make_db().tables["technician_tasks"][1:]],
                         approvals=[{"request_id": 50, "status": state}])
            r, db = await self.post(self.URL, {"decision": "ALLOW"}, db=db, rpc=rpc_ok(outcome="rejected", status="Awaiting Physician"))
            self.assertEqual(db.rpc_calls[0][1]["p_guardrail_decision"], expected, state)
        db = make_db(technician_tasks=[task(1, status="Awaiting Physician", category="medication_request",
                                            requires=True, approval_request_id=50), *make_db().tables["technician_tasks"][1:]],
                     approvals=[{"request_id": 50, "status": "Approved"}, {"request_id": 50, "status": "Approved"}])
        r, db = await self.post(self.URL, {"decision": "ALLOW"}, db=db, rpc=rpc_ok(outcome="rejected", status="Awaiting Physician"))
        self.assertEqual(db.rpc_calls[0][1]["p_guardrail_decision"], "Block")

    async def test_an_approved_clinical_task_gets_an_allow_verdict(self):
        self.user()
        db = make_db(technician_tasks=[task(1, status="Awaiting Physician", category="medication_request",
                                            requires=True, approval_request_id=50), *make_db().tables["technician_tasks"][1:]],
                     approvals=[{"request_id": 50, "status": "Approved"}])
        r, db = await self.post(self.URL, {"decision": "ALLOW"}, db=db)
        self.assertEqual((r.json()["guardrail_decision"], r.json()["guardrail_reason"]), ("Allow", "approved_by_physician"))

    async def test_block_and_hold_require_an_internal_note_and_a_patient_message_before_any_work(self):
        self.user()
        bad = [
            {"decision": "BLOCK"},
            {"decision": "BLOCK", "internal_note": "n"},
            {"decision": "BLOCK", "patient_message": "m"},
            {"decision": "BLOCK", "internal_note": "   ", "patient_message": "m"},
            {"decision": "HOLD", "hold_reason": "needs_information", "internal_note": "n"},
            {"decision": "HOLD", "internal_note": "n", "patient_message": "m"},          # no hold_reason
            {"decision": "ALLOW", "hold_reason": "needs_information"},                  # hold_reason on ALLOW
        ]
        for body in bad:
            with self.subTest(body=body):
                r, db = await self.post(self.URL, body, db=db_with())
                self.assertEqual(r.status_code, 422, r.text)
                self.assertEqual(db.rpc_calls, [])
                self.assertEqual(db.calls, [])  # not even a read

    async def test_validation_rejects_unknown_fields_decisions_and_oversized_text(self):
        self.user()
        for body in ({"decision": "APPROVE"}, {"decision": "ALLOW", "status": "Completed"},
                     {"decision": "ALLOW", "p_actor_user_id": 99}, {"decision": "BLOCK", "internal_note": "x" * 2001, "patient_message": "m"},
                     {"decision": "BLOCK", "internal_note": "n", "patient_message": "m" * 501}, {}):
            r, db = await self.post(self.URL, body, db=db_with())
            self.assertEqual(r.status_code, 422, body)
            self.assertEqual(db.rpc_calls, [])

    async def test_block_sends_notes_trimmed_with_an_allow_verdict(self):
        self.user()
        r, db = await self.post(self.URL, {"decision": "BLOCK", "internal_note": "  Not eligible  ",
                                           "patient_message": " We cannot process this. "}, db=db_with(),
                                rpc=rpc_ok(outcome="recorded", status="Declined", decision="BLOCK"))
        self.assertEqual((r.status_code, r.json()["status"]), (200, "Declined"))
        payload = db.rpc_calls[0][1]
        self.assertEqual((payload["p_internal_note"], payload["p_patient_message"]), ("Not eligible", "We cannot process this."))

    async def test_hold_for_information_is_allow_and_hold_for_physician_is_escalate(self):
        self.user()
        base = {"decision": "HOLD", "internal_note": "n", "patient_message": "m"}
        r, db = await self.post(self.URL, {**base, "hold_reason": "needs_information"}, db=db_with(),
                                rpc=rpc_ok(outcome="recorded", status="On Hold", decision="HOLD"))
        self.assertEqual((r.json()["guardrail_decision"], db.rpc_calls[0][1]["p_hold_reason"]), ("Allow", "needs_information"))
        r, db = await self.post(self.URL, {**base, "hold_reason": "physician_approval"}, db=db_with(),
                                rpc=lambda p: {"task_id": 1, "status": "Awaiting Physician", "latest_decision": "HOLD",
                                               "outcome": "approval_requested", "approval_request_id": 7, "approval_status": "Pending"})
        body = r.json()
        self.assertEqual((body["guardrail_decision"], body["outcome"], body["approval_status"], body["status"]),
                         ("Escalate", "approval_requested", "pending", "Awaiting Physician"))
        self.assertNotIn("approval_request_id", body)

    async def test_invalid_state_is_409_before_the_gateway_or_database_function(self):
        self.user()
        for status, decision, extra in (("Submitted", "ALLOW", {}), ("Completed", "BLOCK", {"internal_note": "n", "patient_message": "m"}),
                                         ("On Hold", "ALLOW", {}), ("In Progress", "ALLOW", {}),
                                         ("Awaiting Physician", "HOLD", {"hold_reason": "needs_information", "internal_note": "n", "patient_message": "m"})):
            r, db = await self.post(self.URL, {"decision": decision, **extra}, db=db_with(status=status))
            self.assertEqual((r.status_code, r.json()["code"]), (409, "invalid_state"), (status, decision))
            self.assertEqual(db.rpc_calls, [])

    async def test_block_while_a_physician_approval_is_pending_is_409(self):
        self.user()
        db = make_db(technician_tasks=[task(1, status="Awaiting Physician", category="medication_request",
                                            requires=True, approval_request_id=50), *make_db().tables["technician_tasks"][1:]])
        r, db = await self.post(self.URL, {"decision": "BLOCK", "internal_note": "n", "patient_message": "m"}, db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "approval_pending"))
        self.assertEqual(db.rpc_calls, [])

    async def test_database_errors_become_safe_responses(self):
        self.user()
        body = {"decision": "ALLOW"}
        cases = [
            (httpx.Response(409, json={"code": "PT409", "message": "SECRET", "details": "approval_required"}), 409, "approval_required"),
            (httpx.Response(404, json={"code": "PT404", "message": "SECRET", "details": "not_found"}), 404, "not_found"),
            (httpx.Response(403, json={"code": "PT403", "message": "SECRET", "details": "permission_missing"}), 403, "permission_missing"),
            (httpx.Response(400, json={"code": "XX000", "message": "SECRET"}), 503, "unavailable"),
            (httpx.Response(504, text="timeout"), 504, "unconfirmed"),
        ]
        for response, status, code in cases:
            r, _ = await self.post(self.URL, body, db=db_with(), rpc=lambda p, resp=response: resp)
            self.assertEqual((r.status_code, r.json()["code"]), (status, code))
            self.assertNotIn("SECRET", r.text)

    async def test_notes_never_reach_audit_logs_from_the_application(self):
        self.user()
        r, db = await self.post(self.URL, {"decision": "BLOCK", "internal_note": "PRIVATE-NOTE", "patient_message": "PUBLIC-MSG"},
                                db=db_with(), rpc=rpc_ok(outcome="recorded", status="Declined", decision="BLOCK"))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("PRIVATE-NOTE", str(db.audits()))
        self.assertNotIn("PUBLIC-MSG", str(db.audits()))


class CompleteTests(WriteBase):
    async def test_complete_in_progress_task(self):
        self.user()
        r, db = await self.post("/api/technician/tasks/1/complete", {"patient_message": "All done"},
                                db=db_with(status="In Progress"), rpc=rpc_ok(status="Completed", decision=None))
        self.assertEqual((r.status_code, r.json()["status"]), (200, "Completed"))
        self.assertEqual(db.rpc_calls[0][1], {"p_task_id": 1, "p_actor_user_id": 6, "p_patient_message": "All done"})

    async def test_complete_requires_in_progress_and_a_granted_approval_where_needed(self):
        self.user()
        r, db = await self.post("/api/technician/tasks/1/complete", db=db_with(status="In Review"))
        self.assertEqual((r.status_code, db.rpc_calls), (409, []))
        clinical = make_db(technician_tasks=[task(1, status="In Progress", category="medication_request", requires=True,
                                                  approval_request_id=50), *make_db().tables["technician_tasks"][1:]],
                           approvals=[{"request_id": 50, "status": "Rejected"}])
        r, db = await self.post("/api/technician/tasks/1/complete", db=clinical)
        self.assertEqual((r.status_code, r.json()["code"], db.rpc_calls), (409, "approval_required", []))


class AccessTests(WriteBase):
    PATHS = [("/api/technician/tasks/1/start-review", None), ("/api/technician/tasks/1/decision", {"decision": "ALLOW"}),
             ("/api/technician/tasks/1/complete", None)]

    async def test_other_roles_and_missing_permission_and_flag_off(self):
        for role in (Role.ADMIN, Role.PATIENT, Role.PHYSICIAN):
            self.user(role=role)
            for path, body in self.PATHS:
                r, db = await self.post(path, body)
                self.assertEqual((r.status_code, db.calls, db.rpc_calls), (403, [], []), (role, path))
        self.user(perms=["view_assigned_task"])  # can read but not update
        for path, body in self.PATHS:
            r, db = await self.post(path, body)
            self.assertEqual((r.status_code, db.rpc_calls), (403, []), path)
        self.user()
        with patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "false"}):
            for path, body in self.PATHS:
                r, db = await self.post(path, body)
                self.assertEqual((r.status_code, r.json()["code"], db.calls), (503, "feature_disabled", []))


if __name__ == "__main__":
    unittest.main()
