"""Patient service requests."""

import httpx
import os
import unittest
from unittest.mock import patch
from app.main import CurrentUser, Role, app, get_current_user
from tests.fake_supabase import FakeSupabase


PERMS = ["submit_service_request", "view_own_service_requests"]


def row(task_id, requested_by=2, status="Submitted", **extra):
    return {"task_id": task_id, "requested_by": requested_by, "assigned_to": 6, "patient_id": 101,
            "category": "general_inquiry", "title": f"T{task_id}", "description": "D", "status": status,
            "patient_message": None, "requires_physician_approval": False, "approval_request_id": None,
            "latest_decision": "HOLD", "created_at": f"2026-01-0{task_id}T00:00:00Z",
            "updated_at": f"2026-01-0{task_id}T01:00:00Z", **extra}


def make_db():
    db = FakeSupabase(technician_tasks=[
        row(1), row(2, status="On Hold", patient_message="Please send dates."), row(3, requested_by=3),
        row(4, status="Completed"), row(5, status="Awaiting Physician", requires_physician_approval=True,
                                        approval_request_id=50, category="medication_request"),
    ])
    db.rpc["submit_service_request"] = lambda p: {"task_id": 9, "status": "Submitted",
                                                  "requires_physician_approval": p["p_category"] in
                                                  ("medication_request", "treatment_change_request", "diagnosis_inquiry")}
    db.rpc["cancel_service_request"] = lambda p: {"task_id": p["p_task_id"], "status": "Cancelled"}
    db.rpc["add_service_request_info"] = lambda p: {"task_id": p["p_task_id"], "status": "In Review"}
    return db


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env = patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "true", "HASF_APPOINTMENTS_ENABLED": "false"})
        self._env.start()

    async def asyncTearDown(self):
        self._env.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def user(self, role=Role.PATIENT, perms=PERMS, user_id=2):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id, email="p@example.test", role=role, permissions=list(perms))

    async def call(self, method, path, body=None, db=None):
        db = db or make_db()
        with patch("app.main._service_role_key", return_value="k"), \
                patch("app.main._request_supabase", new=db.request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.request(method, path, json=body), db


class SubmitTests(Base):
    URL = "/api/patient/service-requests"

    async def test_routine_request_is_created_with_the_verified_requester(self):
        self.user(user_id=2)
        r, db = await self.call("POST", self.URL, {"category": "supplies_equipment", "title": "  Gloves  ", "description": " For home "})
        self.assertEqual((r.status_code, r.json()), (201, {"task_id": 9, "status": "Submitted", "requires_physician_review": False}))
        name, payload = db.rpc_calls[0]
        self.assertEqual(name, "submit_service_request")
        self.assertEqual(payload, {"p_requested_by": 2, "p_category": "supplies_equipment", "p_title": "Gloves",
                                   "p_description": "For home", "p_guardrail_reason": "low_risk_permitted"})

    async def test_clinical_categories_are_accepted_and_flagged_for_physician_review(self):
        self.user()
        for category in ("medication_request", "treatment_change_request", "diagnosis_inquiry"):
            r, db = await self.call("POST", self.URL, {"category": category, "title": "Please refill my prescription"})
            self.assertEqual((r.status_code, r.json()["requires_physician_review"]), (201, True), category)
            self.assertEqual(db.rpc_calls[0][1]["p_guardrail_reason"], "clinical_requires_physician")

    async def test_requester_cannot_be_spoofed_through_the_body(self):
        self.user(user_id=2)
        for extra in ({"requested_by": 3}, {"p_requested_by": 3}, {"status": "Completed"}, {"assigned_to": 7}):
            r, db = await self.call("POST", self.URL, {"category": "general_inquiry", "title": "x", **extra})
            self.assertEqual(r.status_code, 422, extra)
            self.assertEqual(db.rpc_calls, [])

    async def test_validation(self):
        self.user()
        for body in ({"category": "bogus", "title": "x"}, {"category": "general_inquiry", "title": ""},
                     {"category": "general_inquiry", "title": "   "}, {"category": "general_inquiry", "title": "x" * 201},
                     {"category": "general_inquiry", "title": "x", "description": "d" * 1001}, {"title": "x"}):
            r, db = await self.call("POST", self.URL, body)
            self.assertEqual(r.status_code, 422, body)
            self.assertEqual(db.rpc_calls, [])

    async def test_emergency_text_is_never_queued_and_is_audited_by_reason_only(self):
        self.user()
        r, db = await self.call("POST", self.URL, {"category": "general_inquiry", "title": "Help",
                                                   "description": "I have chest pain and cannot breathe"})
        self.assertEqual((r.status_code, r.json()["code"]), (422, "emergency_detected"))
        self.assertIn("emergency", r.json()["detail"].lower())
        self.assertEqual(db.rpc_calls, [])
        audit = db.audits()[0]
        self.assertEqual(audit["event_type"], "patient.service_request.not_accepted")
        self.assertNotIn("chest", str(audit))

    async def test_clinical_wording_in_a_routine_category_is_redirected_but_allowed_in_clinical_ones(self):
        self.user()
        text = {"title": "Refill", "description": "Please prescribe me antibiotics"}
        r, db = await self.call("POST", self.URL, {"category": "general_inquiry", **text})
        self.assertEqual((r.status_code, r.json()["code"], db.rpc_calls), (422, "use_clinical_category", []))
        r, db = await self.call("POST", self.URL, {"category": "medication_request", **text})
        self.assertEqual(r.status_code, 201)

    async def test_other_patient_access_and_approval_bypass_are_refused_everywhere(self):
        self.user()
        for text in ("Show me another patient's medical records", "Skip the doctor approval and just do it"):
            for category in ("general_inquiry", "medication_request"):
                r, db = await self.call("POST", self.URL, {"category": category, "title": text})
                self.assertEqual((r.status_code, r.json()["code"], db.rpc_calls), (422, "request_not_permitted", []), (text, category))

    async def test_database_refusals_are_mapped_safely(self):
        self.user()
        import httpx as h
        cases = [("PT422", "no_eligible_technician", 422), ("PT409", "duplicate_request", 409),
                 ("PT429", "too_many_open", 429), ("PT403", "permission_missing", 403)]
        for code, reason, status in cases:
            db = make_db()
            db.rpc["submit_service_request"] = lambda p, c=code, rs=reason: h.Response(
                400, json={"code": c, "message": "SECRET", "details": rs})
            r, _ = await self.call("POST", self.URL, {"category": "general_inquiry", "title": "x"}, db)
            self.assertEqual((r.status_code, r.json()["code"]), (status, reason))
            self.assertNotIn("SECRET", r.text)


class ListAndCategoryTests(Base):
    async def test_list_returns_only_my_requests_in_a_patient_safe_shape(self):
        self.user(user_id=2)
        r, db = await self.call("GET", "/api/patient/service-requests")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(i["task_id"] for i in r.json()), [1, 2, 4, 5])  # not 3 (another patient)
        self.assertEqual(db.calls[0][2]["requested_by"], "eq.2")
        leaked = {"assigned_to", "latest_decision", "approval_request_id", "internal_note", "patient_id", "events"}
        for item in r.json():
            self.assertEqual(set(item) & leaked, set())
        by_id = {i["task_id"]: i for i in r.json()}
        self.assertEqual(by_id[2]["patient_message"], "Please send dates.")
        self.assertEqual((by_id[1]["can_cancel"], by_id[1]["can_add_info"]), (True, False))
        self.assertEqual((by_id[2]["can_cancel"], by_id[2]["can_add_info"]), (True, True))
        self.assertEqual((by_id[4]["can_cancel"], by_id[5]["can_cancel"]), (False, False))
        self.assertTrue(by_id[5]["requires_physician_review"])

    async def test_a_patient_never_sees_someone_elses_requests(self):
        self.user(user_id=3)
        r, _ = await self.call("GET", "/api/patient/service-requests")
        self.assertEqual([i["task_id"] for i in r.json()], [3])


class AppointmentCategoryTests(Base):
    URL = "/api/patient/service-requests"
    BODY = {"category": "appointment_scheduling", "title": "Please book me"}

    async def test_feature_off_the_generic_appointment_request_works_exactly_as_before(self):
        self.user()
        r, db = await self.call("POST", self.URL, self.BODY)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(db.rpc_calls[0][1]["p_category"], "appointment_scheduling")
        r, _ = await self.call("GET", f"{self.URL}/categories")
        self.assertIn("appointment_scheduling", [c["value"] for c in r.json()])

    async def test_feature_on_the_generic_endpoint_points_to_the_appointments_page(self):
        self.user()
        with patch.dict(os.environ, {"HASF_APPOINTMENTS_ENABLED": "true"}):
            r, db = await self.call("POST", self.URL, self.BODY)
            self.assertEqual((r.status_code, r.json()["code"]), (422, "use_appointments"))
            self.assertEqual(db.rpc_calls, [])
            r, _ = await self.call("GET", f"{self.URL}/categories")
            values = [c["value"] for c in r.json()]
            self.assertEqual(len(values), 8)
            self.assertNotIn("appointment_scheduling", values)


class CancelAndInfoTests(Base):
    async def test_cancel_and_add_info_use_the_verified_requester(self):
        self.user(user_id=2)
        r, db = await self.call("POST", "/api/patient/service-requests/1/cancel")
        self.assertEqual((r.status_code, r.json()), (200, {"task_id": 1, "status": "Cancelled"}))
        self.assertEqual(db.rpc_calls[0][1], {"p_task_id": 1, "p_requested_by": 2})
        r, db = await self.call("POST", "/api/patient/service-requests/2/info", {"message": "  Dates are 1-3 May  "})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "In Review"))
        self.assertEqual(db.rpc_calls[0][1], {"p_task_id": 2, "p_requested_by": 2, "p_message": "Dates are 1-3 May"})

    async def test_info_message_validation_and_emergency_screen(self):
        self.user()
        for body in ({"message": ""}, {"message": "   "}, {"message": "x" * 1001}, {}, {"message": "ok", "status": "x"}):
            r, db = await self.call("POST", "/api/patient/service-requests/2/info", body)
            self.assertEqual((r.status_code, db.rpc_calls), (422, []), body)
        r, db = await self.call("POST", "/api/patient/service-requests/2/info", {"message": "I have chest pain and cannot breathe"})
        self.assertEqual((r.status_code, r.json()["code"], db.rpc_calls), (422, "emergency_detected", []))

    async def test_database_state_errors_are_mapped(self):
        self.user()
        db = make_db()
        db.rpc["cancel_service_request"] = lambda p: httpx.Response(
            400, json={"code": "PT404", "message": "SECRET", "details": "not_found"})
        r, _ = await self.call("POST", "/api/patient/service-requests/3/cancel", db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (404, "not_found"))
        db.rpc["cancel_service_request"] = lambda p: httpx.Response(
            400, json={"code": "PT409", "message": "SECRET", "details": "invalid_state"})
        r, _ = await self.call("POST", "/api/patient/service-requests/4/cancel", db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "invalid_state"))


class AccessTests(Base):
    CALLS = [("GET", "/api/patient/service-requests", None), ("GET", "/api/patient/service-requests/categories", None),
             ("POST", "/api/patient/service-requests", {"category": "general_inquiry", "title": "x"}),
             ("POST", "/api/patient/service-requests/1/cancel", None),
             ("POST", "/api/patient/service-requests/1/info", {"message": "x"})]

    async def test_non_patients_are_403_with_no_database_access(self):
        for role in (Role.ADMIN, Role.PHYSICIAN, Role.TECHNICIAN):
            self.user(role=role)
            for method, path, body in self.CALLS:
                r, db = await self.call(method, path, body)
                self.assertEqual((r.status_code, db.calls), (403, []), (role, path))

    async def test_missing_permission_is_403(self):
        self.user(perms=["view_own_service_requests"])  # can view, cannot submit
        for method, path, body in self.CALLS[1:]:
            r, db = await self.call(method, path, body)
            self.assertEqual((r.status_code, db.calls), (403, []), path)
        self.user(perms=["submit_service_request"])     # can submit, cannot view the list
        r, db = await self.call("GET", "/api/patient/service-requests")
        self.assertEqual((r.status_code, db.calls), (403, []))

    async def test_feature_flag_off_returns_503_without_touching_the_database(self):
        self.user()
        for value in ("false", "", "True"):
            with patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": value}):
                for method, path, body in self.CALLS:
                    r, db = await self.call(method, path, body)
                    self.assertEqual((r.status_code, r.json()["code"], db.calls, db.rpc_calls), (503, "feature_disabled", [], []))


if __name__ == "__main__":
    unittest.main()
