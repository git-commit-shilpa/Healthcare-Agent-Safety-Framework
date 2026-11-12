"""Technician-first correction workflow; only the Physician can approve (the API never does)."""

import httpx
import os
import unittest
from unittest.mock import patch
from app.main import CurrentUser, Role, app, get_current_user
from tests.fake_supabase import FakeSupabase
from tests.test_technician import PERMS as TECH_PERMS, make_db, task


URL = "/api/technician/tasks"


SECRET = "ZZQ-FICTIONAL-CORRECTION-TEXT"


CORR_TASK, CORR_REQ = 30, 500


PATIENT_PERMS = ["view_own_correction_requests", "submit_record_correction", "submit_service_request",
                 "view_own_service_requests"]


def correction_db(task_status="In Review", approval="Pending", reviewed_at=None):
    """approval=None means the Technician-first state: the correction exists but no Physician approval does yet."""
    db = make_db()
    db.tables["technician_tasks"] += [
        task(CORR_TASK, status=task_status, category="record_correction", requires=True,
             approval_request_id=CORR_REQ, patient_message="With your Physician for review."),
        task(31, status="In Review", category="record_correction", requires=True, approval_request_id=501,
             assigned_to=7),                                                    # another Technician's correction
    ]
    db.tables["ai_requests"] = [
        {"request_id": CORR_REQ, "patient_id": 101, "requested_by": 2, "action": "patient_treatment_correction",
         "request_details": SECRET, "guardrail_decision": "Escalate", "created_at": "2026-10-30T00:00:00+00:00"},
        {"request_id": 501, "patient_id": 101, "requested_by": 2, "action": "patient_treatment_correction",
         "request_details": SECRET, "guardrail_decision": "Escalate", "created_at": "2026-10-31T00:00:00+00:00"},
    ]
    if approval is not None:
        db.tables["approvals"].append({"request_id": CORR_REQ, "status": approval, "reviewed_at": reviewed_at, "reviewer_id": 4})
    db.tables["approvals"].append({"request_id": 501, "status": "Pending", "reviewed_at": None, "reviewer_id": 4})
    def coordinate_result(p):
        action = p["p_action"]
        status = ("Awaiting Physician" if action == "request_physician_authorization" else
                  "On Hold" if action == "request_information" else
                  "Completed" if action == "close" else "Declined")
        return {"task_id": p["p_task_id"], "action": action, "status": status,
                "outcome": "approval_requested" if action == "request_physician_authorization" else "recorded"}

    db.rpc["coordinate_correction_task"] = coordinate_result
    db.rpc["start_technician_task_review"] = lambda p: {"task_id": p["p_task_id"], "status": "In Review"}
    return db


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env = patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "true", "HASF_APPOINTMENTS_ENABLED": "false"})
        self._env.start()
        # Privacy: nothing in these flows may make a real outbound HTTP call (Ollama or anything else). The test
        # client talks to the app in-process and Supabase is faked, so any real network transport is a leak.
        self._net = patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                                 side_effect=AssertionError("no outbound network call (Ollama or otherwise) is allowed here"))
        self._net.start()

    async def asyncTearDown(self):
        self._net.stop()
        self._env.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def user(self, role=Role.TECHNICIAN, perms=TECH_PERMS, user_id=6):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id, email="u@example.test", role=role, permissions=list(perms))

    async def call(self, method, path, body=None, db=None):
        db = db or correction_db()
        with patch("app.main._service_role_key", return_value="k"), \
                patch("app.main._request_supabase", new=db.request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.request(method, path, json=body), db


class TechnicianTasksViewTests(Base):
    async def test_a_correction_appears_in_the_assigned_technicians_task_list(self):
        self.user()
        r, _ = await self.call("GET", URL)
        item = next(i for i in r.json()["items"] if i["task_id"] == CORR_TASK)
        self.assertEqual((item["category"], item["approval_status"]), ("record_correction", "pending"))
        self.assertNotIn(31, [i["task_id"] for i in r.json()["items"]])         # another Technician's task

    async def test_detail_shows_ids_status_and_allowed_actions_but_never_the_correction_text(self):
        self.user()
        r, db = await self.call("GET", f"{URL}/{CORR_TASK}")
        body = r.json()
        self.assertEqual(body["correction"], {"request_id": CORR_REQ, "approval_status": "pending", "reviewed_at": None,
                                              "allowed_actions": ["request_physician_authorization", "request_information"],
                                              "decline_reasons": []})
        self.assertNotIn(SECRET, r.text)
        for _, path, params, _ in db.calls:
            if path.endswith("/ai_requests"):
                self.assertNotIn("request_details", params.get("select", ""))
            if path.endswith("/approvals"):
                self.assertNotIn("reviewer_id", params.get("select", ""))     # who decided stays with the Physician
        self.assertNotIn("reviewer", r.text)

    async def test_allowed_actions_follow_the_status_and_the_physicians_decision(self):
        self.user()
        cases = [
            ("Submitted", "Pending", []), ("In Review", "Pending", ["request_physician_authorization", "request_information"]),
            ("Awaiting Physician", "Pending", ["request_information"]), ("On Hold", "Pending", []),
            ("Awaiting Physician", "Approved", ["close"]), ("In Review", "Rejected", ["close"]),
        ]
        for status, approval, expected in cases:
            reviewed = None if approval == "Pending" else "2026-11-01T00:00:00+00:00"
            r, _ = await self.call("GET", f"{URL}/{CORR_TASK}", db=correction_db(status, approval, reviewed))
            self.assertEqual(r.json()["correction"]["allowed_actions"], expected, (status, approval))

    async def test_ordinary_tasks_have_no_correction_block(self):
        self.user()
        r, _ = await self.call("GET", f"{URL}/1")
        self.assertIsNone(r.json()["correction"])

    async def test_an_unassigned_technician_gets_404_and_no_correction_data(self):
        self.user(user_id=8)
        r, _ = await self.call("GET", f"{URL}/{CORR_TASK}")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(SECRET, r.text)
        r, _ = await self.call("GET", f"{URL}/31")                                # exists, but assigned to Technician 7
        self.assertEqual(r.status_code, 404)


class ClinicalActionRefusalTests(Base):
    async def test_allow_block_hold_and_complete_are_refused_for_corrections_before_any_rpc(self):
        self.user()
        for decision in ({"decision": "ALLOW"},
                         {"decision": "BLOCK", "internal_note": "n", "patient_message": "Cannot do that."},
                         {"decision": "HOLD", "hold_reason": "physician_approval", "internal_note": "n", "patient_message": "A physician will review."}):
            r, db = await self.call("POST", f"{URL}/{CORR_TASK}/decision", decision)
            self.assertEqual((r.status_code, r.json()["code"]), (409, "use_correction_coordination"), decision)
            self.assertEqual(db.rpc_calls, [])
        r, db = await self.call("POST", f"{URL}/{CORR_TASK}/complete", {})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "use_correction_coordination"))
        self.assertEqual(db.rpc_calls, [])

    async def test_the_physician_decision_is_never_written_by_the_api(self):
        self.user()
        _, db = await self.call("POST", f"{URL}/{CORR_TASK}/correction-action",
                                {"action": "ready_for_review", "internal_note": "complete"})
        for method, path, _, _ in db.calls:
            if "/approvals" in path or "/ai_requests" in path:
                self.assertEqual(method, "GET")
        self.assertEqual([n for n, _ in db.rpc_calls], ["coordinate_correction_task"])

    async def test_start_review_still_works_for_a_correction(self):
        self.user()
        r, db = await self.call("POST", f"{URL}/{CORR_TASK}/start-review", db=correction_db("Submitted"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.rpc_calls[0][0], "start_technician_task_review")


class CoordinationActionTests(Base):
    def post(self, body, db=None, task_id=CORR_TASK):
        return self.call("POST", f"{URL}/{task_id}/correction-action", body, db)

    async def test_ready_for_review_calls_the_rpc_as_the_verified_technician(self):
        self.user(user_id=6)
        r, db = await self.post({"action": "ready_for_review", "internal_note": "  Details complete.  "})
        self.assertEqual((r.status_code, r.json()["status"], r.json()["action"]), (200, "Awaiting Physician", "ready_for_review"))
        self.assertEqual(db.rpc_calls[0], ("coordinate_correction_task", {
            "p_task_id": CORR_TASK, "p_actor_user_id": 6, "p_action": "request_physician_authorization",
            "p_internal_note": "Details complete.", "p_patient_message": None}))

    async def test_the_actor_comes_from_the_token_never_from_the_body(self):
        self.user(user_id=6)
        r, db = await self.post({"action": "ready_for_review", "internal_note": "n", "actor_user_id": 7})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(db.rpc_calls, [])

    async def test_validation_before_the_rpc(self):
        self.user()
        for body, code in (
            ({"action": "ready_for_review"}, "note_required"),
            ({"action": "request_information", "patient_message": "What changed and when?"}, "note_required"),
            ({"action": "request_information", "internal_note": "n", "patient_message": "Why?"}, "information_request_too_short"),
        ):
            r, db = await self.post(body)
            self.assertEqual((r.status_code, r.json()["code"]), (422, code), body)
            self.assertEqual(db.rpc_calls, [])
        r, db = await self.post({"action": "approve"})                             # no such action exists
        self.assertEqual(r.status_code, 422)
        self.assertEqual(db.rpc_calls, [])

    async def test_request_information_sends_the_question_to_the_patient(self):
        self.user()
        r, db = await self.post({"action": "request_information", "internal_note": "Missing date.",
                                 "patient_message": "Which date did this treatment change?"})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "On Hold"))
        self.assertEqual(db.rpc_calls[0][1]["p_patient_message"], "Which date did this treatment change?")

    async def test_close_needs_a_final_physician_decision(self):
        self.user()
        r, db = await self.post({"action": "close"})
        self.assertEqual((r.status_code, r.json()["code"]), (409, "approval_pending"))
        self.assertEqual(db.rpc_calls, [])
        r, db = await self.post({"action": "close"}, db=correction_db("Awaiting Physician", "Approved", "2026-11-01T00:00:00+00:00"))
        self.assertEqual((r.status_code, r.json()["status"]), (200, "Completed"))
        self.assertEqual(db.rpc_calls[0][0], "coordinate_correction_task")

    async def test_once_decided_only_close_is_possible(self):
        self.user()
        r, db = await self.post({"action": "request_information", "internal_note": "n", "patient_message": "What changed and when?"},
                                db=correction_db("In Review", "Rejected", "2026-11-01T00:00:00+00:00"))
        self.assertEqual((r.status_code, r.json()["code"]), (409, "already_decided"))
        self.assertEqual(db.rpc_calls, [])

    async def test_an_inconsistent_approval_blocks_coordination_without_blocking_anyone_else(self):
        self.user()
        db = correction_db()
        db.tables["approvals"].append({"request_id": CORR_REQ, "status": "Pending", "reviewed_at": None})   # duplicate approval
        r, db = await self.post({"action": "ready_for_review", "internal_note": "n"}, db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "approval_invalid"))
        self.assertEqual(db.rpc_calls, [])

    async def test_another_technicians_or_a_non_correction_task_cannot_be_coordinated(self):
        self.user()
        r, db = await self.post({"action": "ready_for_review", "internal_note": "n"}, task_id=31)
        self.assertEqual(r.status_code, 404)
        r, db = await self.post({"action": "ready_for_review", "internal_note": "n"}, task_id=1)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "not_a_correction_task"))
        self.assertEqual(db.rpc_calls, [])

    async def test_other_roles_and_missing_permissions_are_refused(self):
        for role, perms in ((Role.PATIENT, ["view_own_correction_requests"]), (Role.PHYSICIAN, ["review_escalation"]),
                            (Role.ADMIN, ["manage_users"]), (Role.TECHNICIAN, ["view_assigned_task"])):
            self.user(role=role, perms=perms)
            r, db = await self.post({"action": "ready_for_review", "internal_note": "n"})
            self.assertEqual(r.status_code, 403, role)
            self.assertEqual(db.rpc_calls, [])

    async def test_database_refusal_is_mapped_to_a_safe_message(self):
        self.user()
        db = correction_db()
        db.rpc["coordinate_correction_task"] = lambda p: httpx.Response(
            409, json={"code": "PT409", "message": "SECRET INTERNAL", "details": "correction_coordination_only"})
        r, _ = await self.post({"action": "ready_for_review", "internal_note": "n"}, db=db)
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("SECRET INTERNAL", r.text)


class PatientRequestsTests(Base):
    PATIENT = dict(role=Role.PATIENT, perms=PATIENT_PERMS, user_id=2)

    async def test_the_service_request_list_does_not_repeat_corrections(self):
        self.user(**self.PATIENT)
        db = correction_db()
        db.tables["technician_tasks"].append(task(40, status="Submitted", category="supplies_equipment"))
        r, db = await self.call("GET", "/api/patient/service-requests", db=db)
        self.assertNotIn(CORR_TASK, [i["task_id"] for i in r.json()])
        self.assertIn(40, [i["task_id"] for i in r.json()])
        self.assertEqual(db.calls[0][2]["category"], "neq.record_correction")

    async def test_the_correction_list_carries_the_coordination_status_and_nothing_internal(self):
        self.user(**self.PATIENT)
        db = correction_db("Awaiting Physician")
        db.tables["ai_requests"][0]["requested_by"] = 2
        r, db = await self.call("GET", "/api/patient/correction-requests", db=db)
        self.assertEqual(r.status_code, 200, r.text)
        item = next(i for i in r.json()["items"] if i["request_id"] == CORR_REQ)
        self.assertEqual(item["coordination"], {"task_id": CORR_TASK, "status": "Awaiting Physician",
                                                "status_label": "With your Physician for review.",
                                                "patient_message": "With your Physician for review.", "can_add_info": False})
        self.assertEqual(item["approval_status"], "Pending")
        self.assertNotIn(SECRET, r.text)
        for leaked in ("internal_note", "assigned_to", "reviewer", "requester_note", "actor"):
            self.assertNotIn(leaked, r.text)
        task_call = next(c for c in db.calls if c[1].endswith("/technician_tasks"))
        self.assertEqual(task_call[2]["requested_by"], "eq.2")                 # only the caller's own tasks

    async def test_a_question_from_the_care_team_lets_the_patient_reply(self):
        self.user(**self.PATIENT)
        r, _ = await self.call("GET", "/api/patient/correction-requests", db=correction_db("On Hold"))
        item = next(i for i in r.json()["items"] if i["request_id"] == CORR_REQ)
        self.assertTrue(item["coordination"]["can_add_info"])

    async def test_corrections_without_a_task_still_work(self):
        self.user(**self.PATIENT)
        db = correction_db()
        db.tables["technician_tasks"] = [t for t in db.tables["technician_tasks"] if t["task_id"] != CORR_TASK]
        r, _ = await self.call("GET", "/api/patient/correction-requests", db=db)
        item = next(i for i in r.json()["items"] if i["request_id"] == CORR_REQ)
        self.assertIsNone(item["coordination"])
        self.assertEqual(item["approval_status"], "Pending")

    async def test_a_failure_reading_coordination_never_breaks_the_correction_history(self):
        self.user(**self.PATIENT)
        db = correction_db()
        original = db.request

        async def flaky(path, **kw):
            if path.endswith("/technician_tasks"):
                return httpx.Response(500, json={"message": "boom"})
            return await original(path, **kw)

        with patch("app.main._service_role_key", return_value="k"), patch("app.main._request_supabase", new=flaky):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                r = await c.get("/api/patient/correction-requests")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(next(i for i in r.json()["items"] if i["request_id"] == CORR_REQ)["coordination"])

    async def test_other_patients_corrections_are_never_loaded(self):
        self.user(role=Role.PATIENT, perms=PATIENT_PERMS, user_id=3)
        r, _ = await self.call("GET", "/api/patient/correction-requests")
        self.assertEqual(r.json()["items"], [])

    async def test_a_correction_cannot_be_cancelled_through_service_requests(self):
        self.user(**self.PATIENT)
        db = correction_db()
        db.rpc["cancel_service_request"] = lambda p: {"task_id": p["p_task_id"], "status": "Cancelled"}
        r, db = await self.call("POST", f"/api/patient/service-requests/{CORR_TASK}/cancel", db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "not_cancellable"))
        self.assertEqual(db.rpc_calls, [])

    async def test_replies_to_a_correction_question_are_capped_at_500_characters(self):
        self.user(**self.PATIENT)
        db = correction_db("On Hold")
        db.rpc["add_service_request_info"] = lambda p: {"task_id": p["p_task_id"], "status": "In Review"}
        url = f"/api/patient/service-requests/{CORR_TASK}/info"
        r, db2 = await self.call("POST", url, {"message": "x" * 501}, db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (422, "reply_too_long"))
        self.assertEqual(db2.rpc_calls, [])
        r, db3 = await self.call("POST", url, {"message": "It changed in March."}, db=db)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db3.rpc_calls[0][1]["p_requested_by"], 2)             # the verified patient, not the body
        # An ordinary request keeps its 1000-character allowance.
        db4 = correction_db("On Hold")
        db4.tables["technician_tasks"].append(task(41, status="On Hold", category="supplies_equipment"))
        db4.rpc["add_service_request_info"] = lambda p: {"task_id": p["p_task_id"], "status": "In Review"}
        r, _ = await self.call("POST", "/api/patient/service-requests/41/info", {"message": "y" * 800}, db=db4)
        self.assertEqual(r.status_code, 200)

    async def test_a_patient_cannot_reply_to_someone_elses_correction_task(self):
        self.user(role=Role.PATIENT, perms=PATIENT_PERMS, user_id=3)
        db = correction_db("On Hold")
        db.rpc["add_service_request_info"] = lambda p: httpx.Response(404, json={"code": "PT404", "details": "not_found", "message": "x"})
        r, _ = await self.call("POST", f"/api/patient/service-requests/{CORR_TASK}/info", {"message": "hello there"}, db=db)
        self.assertEqual(r.status_code, 404)


class SubmissionResponseTests(Base):
    PATIENT = dict(role=Role.PATIENT, perms=PATIENT_PERMS, user_id=2)

    async def submit(self, rpc_result):
        self.user(**self.PATIENT)
        db = FakeSupabase(ai_requests=[], approvals=[])
        db.rpc["submit_patient_treatment_correction"] = lambda p: rpc_result
        with patch("app.main._service_role_key", return_value="k"), patch("app.main._request_supabase", new=db.request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.post("/api/patient/correction-requests", json={"proposed_treatment": SECRET}), db

    async def test_the_response_reports_assigned_or_unassigned_and_never_a_task_id_or_the_text(self):
        for value in ("assigned", "unassigned"):
            r, db = await self.submit({"request_id": 9, "guardrail_decision": "Escalate", "approval_status": "Pending",
                                       "coordination": value, "task_id": 77})
            self.assertEqual((r.status_code, r.json()["coordination"]), (200, value))
            self.assertNotIn("task_id", r.text)
            self.assertNotIn(SECRET, r.text)

    async def test_the_rpc_receives_only_the_verified_user_and_the_text_the_patient_confirmed(self):
        r, db = await self.submit({"request_id": 9, "guardrail_decision": "Escalate", "approval_status": "Pending"})
        self.assertEqual(db.rpc_calls[0][1], {"p_requested_by": 2, "p_proposed_treatment": SECRET})


class CategoryIsolationTests(unittest.TestCase):
    def test_record_correction_is_not_a_category_a_patient_can_submit(self):
        from app.service_requests.schemas import ServiceRequestCreate
        from app.technician import catalogue

        self.assertNotIn("record_correction", catalogue.ALL_CATEGORIES)
        with self.assertRaises(Exception):
            ServiceRequestCreate(category="record_correction", title="x")

    def test_the_migration_and_the_code_agree_on_the_new_category_and_actions(self):
        from pathlib import Path

        from app.technician import catalogue

        sql = (Path(__file__).resolve().parents[2] / "database" / "database_setup.sql").read_text(encoding="utf-8")
        self.assertIn(f"'{catalogue.CORRECTION_CATEGORY}'", sql)
        for action in ("ready_for_review", "request_information", "close"):
            self.assertIn(f"'{action}'", sql)
        sql15 = (Path(__file__).resolve().parents[2] / "database" / "database_setup.sql").read_text(encoding="utf-8")
        for reason in catalogue.DECLINE_REASONS:
            self.assertIn(f"'decline_{reason}'", sql15)               # the database accepts exactly the standard reasons


class TechnicianFirstTests(Base):
    """Migration 015: the correction reaches the Technician first; the Physician approval exists only after escalation."""

    def post(self, body, db=None, task_id=CORR_TASK):
        return self.call("POST", f"{URL}/{task_id}/correction-action", body, db)

    DECLINE = {"action": "decline", "reason_code": "duplicate_request", "internal_note": "Same as an earlier request.",
               "patient_message": "We already have this request on file."}

    async def test_before_escalation_the_detail_offers_hold_and_deny_and_never_allow(self):
        self.user()
        r, _ = await self.call("GET", f"{URL}/{CORR_TASK}", db=correction_db(approval=None))
        info = r.json()["correction"]
        self.assertEqual(info["approval_status"], "not_requested")
        self.assertEqual(info["allowed_actions"], ["request_physician_authorization", "request_information", "decline"])
        self.assertEqual([x["code"] for x in info["decline_reasons"]],
                         ["duplicate_request", "incomplete_information", "withdrawn_by_patient", "not_a_correction_request"])
        self.assertNotIn("allow", [a for a in info["allowed_actions"]])
        listed, _ = await self.call("GET", URL, db=correction_db(approval=None))
        self.assertEqual(next(i for i in listed.json()["items"] if i["task_id"] == CORR_TASK)["approval_status"], "not_requested")

    async def test_deny_is_offered_only_before_a_physician_review_exists(self):
        self.user()
        for status, approval, expected in (
            ("In Review", None, True), ("On Hold", None, True), ("Submitted", None, False),
            ("Awaiting Physician", "Pending", False), ("In Review", "Pending", False), ("In Review", "Approved", False),
        ):
            reviewed = "2026-11-01T00:00:00+00:00" if approval == "Approved" else None
            r, _ = await self.call("GET", f"{URL}/{CORR_TASK}", db=correction_db(status, approval, reviewed))
            self.assertEqual("decline" in r.json()["correction"]["allowed_actions"], expected, (status, approval))

    async def test_escalation_asks_the_database_to_create_the_approval_as_the_verified_technician(self):
        self.user(user_id=6)
        r, db = await self.post({"action": "request_physician_authorization", "internal_note": "Complete."}, db=correction_db(approval=None))
        self.assertEqual((r.status_code, r.json()["status"], r.json()["outcome"]), (200, "Awaiting Physician", "approval_requested"))
        self.assertEqual(db.rpc_calls[0][1]["p_actor_user_id"], 6)
        for method, path, _, _ in db.calls:                                     # the API never writes approvals itself
            if "/approvals" in path:
                self.assertEqual(method, "GET")

    async def test_deny_validation_and_mapping(self):
        self.user()
        db = correction_db(approval=None)
        r, db1 = await self.post(self.DECLINE, db=db)
        self.assertEqual((r.status_code, r.json()["status"]), (200, "Declined"))
        self.assertEqual(db1.rpc_calls[0][1]["p_action"], "decline_duplicate_request")
        for body, code in (
            ({**self.DECLINE, "reason_code": None}, "invalid_reason"),
            ({**self.DECLINE, "patient_message": "Too short."}, "rejection_reason_too_short"),
            ({**self.DECLINE, "internal_note": None}, "note_required"),
        ):
            r, db2 = await self.post(body, db=correction_db(approval=None))
            self.assertEqual((r.status_code, r.json()["code"]), (422, code), body)
            self.assertEqual(db2.rpc_calls, [])
        r, _ = await self.post({**self.DECLINE, "reason_code": "clinically_unsafe"}, db=correction_db(approval=None))
        self.assertEqual(r.status_code, 422)                                    # not one of the standard reasons
        r, db3 = await self.post({"action": "request_information", "reason_code": "duplicate_request", "internal_note": "n",
                                  "patient_message": "Which date did this change?"}, db=correction_db(approval=None))
        self.assertEqual((r.status_code, r.json()["code"]), (422, "invalid_request"))

    async def test_deny_is_refused_once_the_physician_has_the_request_or_has_decided(self):
        self.user()
        r, db = await self.post(self.DECLINE, db=correction_db("Awaiting Physician", "Pending"))
        self.assertEqual((r.status_code, r.json()["code"]), (409, "cannot_decline_after_authorization"))
        r, db2 = await self.post(self.DECLINE, db=correction_db("In Review", "Rejected", "2026-11-01T00:00:00+00:00"))
        self.assertEqual((r.status_code, r.json()["code"]), (409, "already_decided"))
        self.assertEqual(db.rpc_calls + db2.rpc_calls, [])

    async def test_no_physician_account_means_a_clear_refusal_and_no_approval(self):
        self.user()
        db = correction_db(approval=None)
        db.rpc["coordinate_correction_task"] = lambda p: httpx.Response(
            409, json={"code": "PT409", "message": "x", "details": "physician_unreachable"})
        r, db = await self.post({"action": "request_physician_authorization", "internal_note": "n"}, db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "physician_unreachable"))
        self.assertIn("no linked account", r.json()["detail"])
        self.assertEqual([c for c in db.calls if "/approvals" in c[1] and c[0] != "GET"], [])
        self.assertTrue(any(r_["event_type"] == "technician.correction.escalation_blocked" for r_ in db.tables.get("audit_logs", [])))

    async def test_a_waiting_correction_shows_a_safe_stage_and_is_never_a_data_error(self):
        self.user(role=Role.PATIENT, perms=PATIENT_PERMS, user_id=2)
        for status, stage in (("Submitted", "with_care_team"), ("In Review", "with_care_team"), ("On Hold", "needs_reply"), ("Declined", "declined")):
            r, _ = await self.call("GET", "/api/patient/correction-requests", db=correction_db(status, approval=None))
            item = next(i for i in r.json()["items"] if i["request_id"] == CORR_REQ)
            self.assertEqual((item["review_stage"], item["approval_status"], item["status_issue"]), (stage, None, None), status)
            self.assertNotIn(SECRET, r.text)
        r, _ = await self.call("GET", "/api/patient/correction-requests", db=correction_db("Awaiting Physician", "Pending"))
        self.assertEqual(next(i for i in r.json()["items"] if i["request_id"] == CORR_REQ)["review_stage"], "with_physician")
        # Without a coordination task the missing approval is still an inconsistency.
        db = correction_db(approval=None)
        db.tables["technician_tasks"] = [t for t in db.tables["technician_tasks"] if t["task_id"] != CORR_TASK]
        r, _ = await self.call("GET", "/api/patient/correction-requests", db=db)
        item = next(i for i in r.json()["items"] if i["request_id"] == CORR_REQ)
        self.assertEqual((item["status_issue"], item["review_stage"]), ("missing_approval", None))

    async def test_a_correction_waiting_for_the_technician_does_not_block_a_second_one_and_neither_does_a_declined_one(self):
        self.user(role=Role.PATIENT, perms=PATIENT_PERMS, user_id=2)
        db = correction_db("In Review", approval=None)
        db.tables["ai_requests"] = [db.tables["ai_requests"][0]]                 # only the one correction
        db.tables["technician_tasks"] = [t for t in db.tables["technician_tasks"] if t["task_id"] != 31]
        db.rpc["submit_patient_treatment_correction"] = lambda p: {"request_id": 9, "guardrail_decision": "Escalate",
                                                                 "approval_status": None, "coordination": "assigned", "task_id": 77}
        r, db = await self.call("POST", "/api/patient/correction-requests", {"proposed_treatment": "A new proposal"}, db=db)
        self.assertEqual((r.status_code, r.json()["request_id"]), (200, 9))
        self.assertEqual([n for n, _ in db.rpc_calls], ["submit_patient_treatment_correction"])
        declined = correction_db("Declined", approval=None)
        declined.tables["ai_requests"] = [declined.tables["ai_requests"][0]]
        declined.tables["technician_tasks"] = [t for t in declined.tables["technician_tasks"] if t["task_id"] != 31]
        declined.rpc["submit_patient_treatment_correction"] = lambda p: {"request_id": 9, "guardrail_decision": "Escalate",
                                                                       "approval_status": None, "coordination": "assigned", "task_id": 77}
        r, declined = await self.call("POST", "/api/patient/correction-requests", {"proposed_treatment": "A new proposal"}, db=declined)
        self.assertEqual((r.status_code, r.json()["approval_status"], r.json()["review_stage"]), (200, None, "with_care_team"))

    async def test_no_technician_fails_closed_with_a_clear_message_and_no_physician_routing(self):
        self.user(role=Role.PATIENT, perms=PATIENT_PERMS, user_id=3)
        db = FakeSupabase(ai_requests=[], approvals=[], technician_tasks=[])
        db.rpc["submit_patient_treatment_correction"] = lambda p: httpx.Response(
            422, json={"code": "PT422", "message": "No technician is currently available for your care team. Please contact your care team.",
                       "details": "no_eligible_technician"})
        r, db = await self.call("POST", "/api/patient/correction-requests", {"proposed_treatment": SECRET}, db=db)
        self.assertEqual(r.status_code, 409)
        self.assertIn("No technician is currently available", r.json()["detail"])
        self.assertIn("Nothing was submitted", r.json()["detail"])
        self.assertNotIn(SECRET, r.text)
        self.assertEqual([n for n, _ in db.rpc_calls], ["submit_patient_treatment_correction"])    # no second, direct-to-Physician attempt
        self.assertTrue(any(a["event_type"] == "patient.correction.not_accepted" for a in db.tables.get("audit_logs", [])))

    async def test_the_new_submission_shape_is_accepted_and_unknown_shapes_fail_closed(self):
        self.user(role=Role.PATIENT, perms=PATIENT_PERMS, user_id=2)
        good = {"request_id": 9, "guardrail_decision": "Escalate", "approval_status": None, "coordination": "assigned", "task_id": 7}
        for result, expected in ((good, 200), ({**good, "coordination": "unassigned"}, 502), ({**good, "approval_status": "Approved"}, 502)):
            db = FakeSupabase(ai_requests=[], approvals=[], technician_tasks=[])
            db.rpc["submit_patient_treatment_correction"] = lambda p, r_=result: r_
            r, _ = await self.call("POST", "/api/patient/correction-requests", {"proposed_treatment": "A proposal"}, db=db)
            self.assertEqual(r.status_code, expected, result)

    async def test_gateway_treats_corrections_as_clinical_and_never_allows(self):
        from app.technician import gateway

        common = dict(permissions=["update_task_status"], category="record_correction", requires_physician_approval=True, current_status="In Review")
        self.assertEqual(gateway.evaluate(decision="ALLOW", hold_reason=None, approval_status="none", **common).decision, "Escalate")
        self.assertEqual(gateway.evaluate(decision="ALLOW", hold_reason=None, approval_status="pending", **common).decision, "Escalate")
        self.assertEqual(gateway.evaluate(decision="HOLD", hold_reason="physician_approval", approval_status="none", **common).decision, "Escalate")
        self.assertEqual(gateway.evaluate(decision="HOLD", hold_reason="needs_information", approval_status="none", **common).decision, "Allow")


if __name__ == "__main__":
    unittest.main()
