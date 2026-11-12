"""Appointment scheduling for Patients, Physicians and Technicians."""

import httpx
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from app.main import CurrentUser, Role, app, get_current_user
from tests.fake_supabase import FakeSupabase
from tests.test_technician import PERMS as TECH_PERMS, make_db as make_tech_db, task as make_tech_task


BASE = "/api/patient/appointments"


PERMS = ["request_appointment", "view_physician_availability", "view_own_service_requests"]


FUTURE = (datetime.now(timezone.utc) + timedelta(days=10)).replace(hour=14, minute=0, second=0, microsecond=0)


FUTURE_ISO = FUTURE.strftime("%Y-%m-%dT%H:%M:%SZ")


END_ISO = (FUTURE + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")


def task(task_id, requested_by=2, status="Submitted", category="appointment_scheduling", **extra):
    return {"task_id": task_id, "requested_by": requested_by, "category": category, "status": status,
            "patient_message": None, "created_at": f"2026-01-0{task_id}T00:00:00Z", **extra}


def appt(task_id, status="Pending Confirmation", starts=FUTURE, physician=4, **extra):
    return {"appointment_id": 100 + task_id, "task_id": task_id, "physician_user_id": physician,
            "starts_at": starts.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "ends_at": (starts + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "physician_timezone": "America/New_York", "reason": "Check-up", "status": status, **extra}


def make_db():
    db = FakeSupabase(
        technician_tasks=[
            task(1), task(2, status="On Hold", patient_message="What is the reason?"),
            task(3, requested_by=3), task(4, status="In Progress", patient_message="Confirmed!"),
            task(5, category="general_inquiry"),
        ],
        appointments=[
            appt(1), appt(2, status="Needs Information"), appt(3, physician=5),
            appt(4, status="Confirmed", confirmed_by=6),
        ],
        users=[{"user_id": 4, "name": "Dr. Fictional"}, {"user_id": 5, "name": "Dr. Other"}],
        technician_task_events=[
            {"event_id": 1, "task_id": 2, "event_type": "created", "decision": None, "hold_reason": None,
             "patient_message": None, "requester_note": None, "created_at": "2026-01-02T00:00:00Z",
             "internal_note": "SECRET INTERNAL", "guardrail_reason": "secret_reason"},
            {"event_id": 2, "task_id": 2, "event_type": "review_started", "decision": None, "hold_reason": None,
             "patient_message": None, "requester_note": None, "created_at": "2026-01-02T01:00:00Z",
             "internal_note": "SECRET INTERNAL"},
            {"event_id": 3, "task_id": 2, "event_type": "decision", "decision": "HOLD", "hold_reason": "needs_information",
             "patient_message": "What is the reason?", "requester_note": None, "created_at": "2026-01-02T02:00:00Z",
             "internal_note": "SECRET INTERNAL"},
        ],
    )
    db.rpc["list_bookable_physicians"] = lambda p: {"physicians": [
        {"user_id": 4, "name": "Dr. Fictional", "timezone": "America/New_York"}]}
    db.rpc["list_physician_availability"] = lambda p: {
        "physician_id": p["p_physician_id"], "timezone": "America/New_York",
        "slots": [{"start": FUTURE_ISO, "end": END_ISO}]}
    db.rpc["submit_appointment_request"] = lambda p: {
        "task_id": 9, "appointment_id": 109, "status": "Pending Confirmation", "task_status": "Submitted",
        "starts_at": p["p_starts_at"], "ends_at": END_ISO, "timezone": "America/New_York"}
    db.rpc["cancel_service_request"] = lambda p: {"task_id": p["p_task_id"], "status": "Cancelled"}
    db.rpc["add_service_request_info"] = lambda p: {"task_id": p["p_task_id"], "status": "In Review"}
    return db


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env = patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "true", "HASF_APPOINTMENTS_ENABLED": "true"})
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


class AccessTests(Base):
    async def test_feature_flag_is_fail_closed(self):
        self.user()
        for env in ({"HASF_APPOINTMENTS_ENABLED": ""}, {"HASF_SERVICE_REQUESTS_ENABLED": ""}):
            with patch.dict(os.environ, env):
                r, db = await self.call("GET", f"{BASE}/physicians")
                self.assertEqual((r.status_code, r.json()["code"]), (503, "feature_disabled"))
                self.assertEqual(db.calls, [])

    async def test_non_patients_are_refused_before_any_data_is_touched(self):
        for role in (Role.TECHNICIAN, Role.PHYSICIAN, Role.ADMIN):
            self.user(role=role)
            for method, path in (("GET", f"{BASE}/physicians"), ("GET", BASE), ("POST", BASE)):
                r, db = await self.call(method, path, {} if method == "POST" else None)
                self.assertEqual(r.status_code, 403, (role, path))
                self.assertEqual(db.calls, [])

    async def test_missing_permission_rows_are_refused(self):
        self.user(perms=["view_own_service_requests"])
        r, db = await self.call("GET", f"{BASE}/physicians")
        self.assertEqual(r.status_code, 403)
        r, db = await self.call("POST", BASE, {"physician_id": 4, "starts_at": FUTURE_ISO, "confirmed": True})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(db.rpc_calls, [])


class BrowseTests(Base):
    async def test_doctors_come_from_the_database_not_from_code(self):
        self.user()
        db = make_db()
        db.rpc["list_bookable_physicians"] = lambda p: {"physicians": [
            {"user_id": 11, "name": "Dr. Zed Fictional", "timezone": "Europe/London"}]}
        r, db = await self.call("GET", f"{BASE}/physicians", db=db)
        self.assertEqual(r.json(), [{"physician_id": 11, "name": "Dr. Zed Fictional", "timezone": "Europe/London"}])
        self.assertEqual([c[0] for c in db.rpc_calls], ["list_bookable_physicians"])

    async def test_no_doctors_means_an_empty_list(self):
        self.user()
        db = make_db()
        db.rpc["list_bookable_physicians"] = lambda p: {"physicians": []}
        r, _ = await self.call("GET", f"{BASE}/physicians", db=db)
        self.assertEqual((r.status_code, r.json()), (200, []))

    async def test_availability_is_read_from_the_database_and_books_nothing(self):
        self.user()
        day = FUTURE.date().isoformat()
        r, db = await self.call("GET", f"{BASE}/physicians/4/availability?date={day}")
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["slots"][0]["start"], FUTURE_ISO)
        self.assertTrue(body["slots"][0]["local_label"].endswith(("EST", "EDT")))
        self.assertEqual([c[0] for c in db.rpc_calls], ["list_physician_availability"])
        self.assertEqual(db.rpc_calls[0][1], {"p_physician_id": 4, "p_from": day, "p_to": day})
        self.assertEqual([c for c in db.calls if c[0] == "POST" and "rpc" not in c[1]], [])  # nothing written

    async def test_invalid_date_is_rejected(self):
        self.user()
        for bad in ("not-a-date", "2026-13-40", ""):
            r, db = await self.call("GET", f"{BASE}/physicians/4/availability?date={bad}")
            self.assertEqual(r.status_code, 422, bad)
            self.assertEqual(db.rpc_calls, [])
        r, _ = await self.call("GET", f"{BASE}/physicians/4/availability")
        self.assertEqual(r.status_code, 422)

    async def test_unknown_doctor_is_not_found(self):
        self.user()
        db = make_db()
        db.rpc["list_physician_availability"] = lambda p: httpx.Response(
            404, json={"code": "PT404", "message": "Doctor not found.", "details": "not_found"})
        r, _ = await self.call("GET", f"{BASE}/physicians/999/availability?date=2030-01-01", db=db)
        self.assertEqual(r.status_code, 404)


class SubmitTests(Base):
    def body(self, **extra):
        return {"physician_id": 4, "starts_at": FUTURE_ISO, "reason": "  Annual check-up  ", "confirmed": True, **extra}

    async def test_submit_creates_a_pending_request_and_never_claims_a_booking(self):
        self.user(user_id=2)
        r, db = await self.call("POST", BASE, self.body())
        self.assertEqual(r.status_code, 201)
        out = r.json()
        self.assertEqual((out["request_id"], out["status"], out["status_label"], out["booked"]),
                         (9, "Pending Confirmation", "Pending Technician Confirmation", False))
        self.assertIn("NOT booked", out["message"])
        name, payload = db.rpc_calls[0]
        self.assertEqual(name, "submit_appointment_request")
        self.assertEqual(payload, {"p_requested_by": 2, "p_physician_id": 4, "p_starts_at": FUTURE_ISO,
                                   "p_reason": "Annual check-up", "p_guardrail_reason": "low_risk_permitted"})

    async def test_explicit_confirmation_is_required(self):
        self.user()
        for confirmed in (False,):
            r, db = await self.call("POST", BASE, self.body(confirmed=confirmed))
            self.assertEqual((r.status_code, r.json()["code"]), (422, "confirmation_required"))
            self.assertEqual(db.rpc_calls, [])
        body = self.body()
        del body["confirmed"]
        r, db = await self.call("POST", BASE, body)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(db.rpc_calls, [])

    async def test_requester_cannot_be_spoofed_and_unknown_fields_are_rejected(self):
        self.user(user_id=2)
        for extra in ({"requested_by": 3}, {"p_requested_by": 3}, {"status": "Confirmed"}, {"assigned_to": 7},
                      {"task_id": 1}):
            r, db = await self.call("POST", BASE, self.body(**extra))
            self.assertEqual(r.status_code, 422, extra)
            self.assertEqual(db.rpc_calls, [], extra)

    async def test_invalid_times_are_rejected_before_the_database(self):
        self.user()
        for bad in ("2026-11-20T10:00:00", "tomorrow", "", None, 12345678):   # naive / not a date
            r, db = await self.call("POST", BASE, self.body(starts_at=bad))
            self.assertEqual(r.status_code, 422, bad)
            self.assertEqual(db.rpc_calls, [], bad)
        r, db = await self.call("POST", BASE, self.body(physician_id=0))
        self.assertEqual(r.status_code, 422)
        r, db = await self.call("POST", BASE, self.body(reason="x" * 501))
        self.assertEqual(r.status_code, 422)

    async def test_offset_times_are_normalised_to_utc(self):
        self.user()
        r, db = await self.call("POST", BASE, self.body(starts_at="2030-06-01T10:00:00-04:00"))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(db.rpc_calls[0][1]["p_starts_at"], "2030-06-01T14:00:00Z")

    async def test_clinical_or_emergency_reason_is_refused_and_only_a_reason_code_is_audited(self):
        self.user()
        r, db = await self.call("POST", BASE, self.body(reason="I have crushing chest pain right now"))
        self.assertEqual((r.status_code, r.json()["code"]), (422, "emergency_detected"))
        self.assertEqual(db.rpc_calls, [])
        r, db = await self.call("POST", BASE, self.body(reason="please prescribe me 50mg of oxycodone"))
        self.assertEqual((r.status_code, r.json()["code"]), (422, "reason_not_accepted"))
        self.assertEqual(db.rpc_calls, [])
        for audit in db.audits():
            self.assertNotIn("oxycodone", str(audit))
            self.assertNotIn("chest", str(audit))

    async def test_conflicts_and_stale_slots_map_to_safe_messages(self):
        self.user()
        cases = [("slot_unavailable", 409), ("patient_overlap", 409), ("duplicate_request", 409),
                 ("too_many_open", 429), ("no_eligible_technician", 422), ("invalid_date", 422)]
        for reason, status in cases:
            db = make_db()
            db.rpc["submit_appointment_request"] = lambda p, reason=reason, status=status: httpx.Response(
                400, json={"code": {409: "PT409", 429: "PT429", 422: "PT422"}[status],
                           "message": "internal sql text", "details": reason, "hint": "secret"})
            r, _ = await self.call("POST", BASE, self.body(), db=db)
            self.assertEqual((r.status_code, r.json()["code"]), (status, reason))
            self.assertNotIn("internal sql text", r.text)
            self.assertNotIn("secret", r.text)

    async def test_unconfirmed_outcome_is_reported_honestly(self):
        self.user()
        db = make_db()
        db.rpc["submit_appointment_request"] = lambda p: httpx.Response(504)
        r, _ = await self.call("POST", BASE, self.body(), db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (504, "unconfirmed"))


class ListAndDetailTests(Base):
    async def test_only_my_appointments_are_listed_with_groups_and_labels(self):
        self.user(user_id=2)
        r, db = await self.call("GET", BASE)
        items = {i["request_id"]: i for i in r.json()}
        self.assertEqual(sorted(items), [1, 2, 4])                     # not 3 (other patient), not 5 (not an appointment)
        self.assertEqual((items[1]["status_label"], items[1]["group"]), ("Pending Technician Confirmation", "pending"))
        self.assertEqual((items[2]["group"], items[2]["can_add_info"], items[2]["can_cancel"]), ("pending", True, True))
        self.assertEqual((items[4]["group"], items[4]["can_cancel"]), ("upcoming", False))
        self.assertEqual(items[1]["physician_name"], "Dr. Fictional")
        task_query = [c for c in db.calls if c[1].endswith("/technician_tasks")][0][2]
        self.assertEqual(task_query["requested_by"], "eq.2")
        self.assertEqual(task_query["category"], "eq.appointment_scheduling")

    async def test_listing_never_leaks_internal_fields(self):
        self.user()
        r, _ = await self.call("GET", BASE)
        text = r.text
        for forbidden in ("assigned_to", "internal_note", "confirmed_by", "guardrail", "latest_decision",
                          "approval_request_id", "technician"):
            self.assertNotIn(forbidden, text)

    async def test_status_groups(self):
        from app.scheduling.service import group_for
        now = datetime.now(timezone.utc)
        future, past = (now + timedelta(days=1)).isoformat(), (now - timedelta(days=1)).isoformat()
        self.assertEqual(group_for("Pending Confirmation", future), "pending")
        self.assertEqual(group_for("Needs Information", future), "pending")
        self.assertEqual(group_for("Awaiting Physician", future), "pending")
        self.assertEqual(group_for("Confirmed", future), "upcoming")
        self.assertEqual(group_for("Confirmed", past), "completed")
        self.assertEqual(group_for("Completed", past), "completed")
        self.assertEqual(group_for("Rejected", future), "rejected")
        self.assertEqual(group_for("Cancelled", future), "rejected")

    async def test_detail_timeline_shows_patient_visible_events_only(self):
        self.user(user_id=2)
        r, _ = await self.call("GET", f"{BASE}/2")
        self.assertEqual(r.status_code, 200)
        kinds = [t["kind"] for t in r.json()["timeline"]]
        self.assertEqual(kinds, ["submitted", "info_requested"])        # review_started is internal
        self.assertEqual(r.json()["timeline"][1]["text"], "What is the reason?")
        for forbidden in ("SECRET INTERNAL", "secret_reason", "internal_note", "guardrail"):
            self.assertNotIn(forbidden, r.text)

    async def test_detail_of_someone_elses_or_non_appointment_request_is_404(self):
        self.user(user_id=2)
        for task_id in (3, 5, 999):
            r, _ = await self.call("GET", f"{BASE}/{task_id}")
            self.assertEqual((r.status_code, r.json()["code"]), (404, "not_found"), task_id)


class CancelAndRespondTests(Base):
    async def test_cancel_uses_the_verified_requester(self):
        self.user(user_id=2)
        r, db = await self.call("POST", f"{BASE}/1/cancel")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "Cancelled"))
        self.assertEqual(db.rpc_calls[0], ("cancel_service_request", {"p_task_id": 1, "p_requested_by": 2}))

    async def test_cannot_cancel_or_reply_to_other_peoples_or_non_appointment_requests(self):
        self.user(user_id=2)
        for task_id in (3, 5):
            r, db = await self.call("POST", f"{BASE}/{task_id}/cancel")
            self.assertEqual(r.status_code, 404, task_id)
            self.assertEqual(db.rpc_calls, [])
            r, db = await self.call("POST", f"{BASE}/{task_id}/respond", {"message": "hello"})
            self.assertEqual(r.status_code, 404, task_id)
            self.assertEqual(db.rpc_calls, [])

    async def test_respond_resumes_review_and_is_screened(self):
        self.user(user_id=2)
        r, db = await self.call("POST", f"{BASE}/2/respond", {"message": "  It is my annual check-up  "})
        self.assertEqual((r.status_code, r.json()["status_label"]), (200, "Pending Technician Confirmation"))
        self.assertEqual(db.rpc_calls[0][1], {"p_task_id": 2, "p_requested_by": 2, "p_message": "It is my annual check-up"})
        r, db = await self.call("POST", f"{BASE}/2/respond", {"message": "I have crushing chest pain right now"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(db.rpc_calls, [])
        r, db = await self.call("POST", f"{BASE}/2/respond", {"message": "   "})
        self.assertEqual(r.status_code, 422)


class FormattingTests(unittest.TestCase):
    def test_local_label_uses_the_doctors_timezone_and_handles_dst(self):
        from app.scheduling.service import local_label
        self.assertEqual(local_label("2027-03-08T14:00:00Z", "America/New_York"), "Mon, 08 Mar 2027, 9:00 AM EST")
        self.assertEqual(local_label("2027-03-15T13:00:00Z", "America/New_York"), "Mon, 15 Mar 2027, 9:00 AM EDT")
        self.assertEqual(local_label("2027-03-15T00:30:00Z", "Europe/London"), "Mon, 15 Mar 2027, 12:30 AM GMT")

    def test_unknown_timezone_falls_back_to_utc(self):
        from app.scheduling.service import local_label
        self.assertTrue(local_label("2027-03-15T13:00:00Z", "Mars/Base").endswith("UTC"))
        self.assertTrue(local_label("2027-03-15T13:00:00Z", None).endswith("UTC"))


class IsolationFromAiTests(unittest.TestCase):
    def test_scheduling_package_never_imports_or_calls_an_ai_model(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1] / "app" / "scheduling"
        source = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py")).lower()
        for banned in ("ollama", "nemoguardrails", "intent_recognizer", "patient_chat", "generate_reply",
                       "qwen", "openai", "anthropic"):
            self.assertNotIn(banned, source)


PHYSICIAN_BASE = "/api/physician/schedule"


PHYSICIAN_PERMS = ["manage_own_schedule", "review_escalation"]


RULE = {"weekday": 1, "start_time": "09:00", "end_time": "12:00", "slot_minutes": 30, "timezone": "America/New_York"}


EXC = {"kind": "unavailable", "starts_at": "2030-06-03T13:00:00Z", "ends_at": "2030-06-03T17:00:00Z", "reason": "Conference"}


def make_physician_db():
    db = FakeSupabase(
        physician_schedule_rules=[
            {"rule_id": 1, "physician_user_id": 4, "weekday": 1, "start_minute": 540, "end_minute": 720,
             "slot_minutes": 30, "timezone": "America/New_York", "effective_from": "2026-01-01",
             "effective_to": None, "is_active": True},
            {"rule_id": 2, "physician_user_id": 5, "weekday": 2, "start_minute": 600, "end_minute": 660,
             "slot_minutes": 30, "timezone": "Europe/London", "effective_from": "2026-01-01",
             "effective_to": None, "is_active": True},
        ],
        physician_schedule_exceptions=[
            {"exception_id": 1, "physician_user_id": 4, "kind": "unavailable", "starts_at": "2030-06-03T13:00:00+00:00",
             "ends_at": "2030-06-03T17:00:00+00:00", "slot_minutes": None, "reason": "Conference", "is_active": True},
            {"exception_id": 2, "physician_user_id": 4, "kind": "unavailable", "starts_at": "2030-07-03T13:00:00+00:00",
             "ends_at": "2030-07-03T17:00:00+00:00", "slot_minutes": None, "reason": "Old", "is_active": False},
            {"exception_id": 3, "physician_user_id": 5, "kind": "unavailable", "starts_at": "2030-06-03T13:00:00+00:00",
             "ends_at": "2030-06-03T17:00:00+00:00", "slot_minutes": None, "reason": "Other doctor", "is_active": True},
        ],
        appointments=[
            {"appointment_id": 1, "task_id": 11, "patient_id": 101, "physician_user_id": 4,
             "starts_at": "2030-06-03T14:00:00+00:00", "ends_at": "2030-06-03T14:30:00+00:00",
             "physician_timezone": "America/New_York", "status": "Confirmed", "reason": "PRIVATE REASON"},
            {"appointment_id": 2, "task_id": 12, "patient_id": 102, "physician_user_id": 5,
             "starts_at": "2030-06-03T15:00:00+00:00", "ends_at": "2030-06-03T15:30:00+00:00",
             "physician_timezone": "Europe/London", "status": "Confirmed", "reason": "x"},
        ],
        patients=[{"patient_id": 101, "user_id": 2}, {"patient_id": 102, "user_id": 3}],
        users=[{"user_id": 2, "name": "Fictional Patient A"}, {"user_id": 3, "name": "Fictional Patient B"}],
    )
    db.rpc["upsert_schedule_rule"] = lambda p: {"rule_id": p["p_rule_id"] or 9}
    db.rpc["deactivate_schedule_rule"] = lambda p: {"rule_id": p["p_rule_id"], "is_active": False}
    db.rpc["add_schedule_exception"] = lambda p: {"exception_id": 9}
    db.rpc["remove_schedule_exception"] = lambda p: {"exception_id": p["p_exception_id"], "is_active": False}
    return db


class PhysicianBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env = patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "true", "HASF_APPOINTMENTS_ENABLED": "true"})
        self._env.start()

    async def asyncTearDown(self):
        self._env.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def user(self, role=Role.PHYSICIAN, perms=PHYSICIAN_PERMS, user_id=4):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id, email="d@example.test", role=role, permissions=list(perms))

    async def call(self, method, path, body=None, db=None):
        db = db or make_physician_db()
        with patch("app.main._service_role_key", return_value="k"), \
                patch("app.main._request_supabase", new=db.request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.request(method, path, json=body), db


class PhysicianAccessTests(PhysicianBase):
    async def test_only_physicians_with_the_permission_and_flag(self):
        for role in (Role.PATIENT, Role.TECHNICIAN, Role.ADMIN):
            self.user(role=role)
            for method, path in (("GET", f"{PHYSICIAN_BASE}/rules"), ("POST", f"{PHYSICIAN_BASE}/rules"), ("GET", f"{PHYSICIAN_BASE}/appointments")):
                r, db = await self.call(method, path, RULE if method == "POST" else None)
                self.assertEqual(r.status_code, 403, (role, path))
                self.assertEqual((db.calls, db.rpc_calls), ([], []))
        self.user(perms=["review_escalation"])            # Admin has not enabled scheduling for Physicians
        r, db = await self.call("GET", f"{PHYSICIAN_BASE}/rules")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(db.calls, [])


class RuleTests(PhysicianBase):
    async def test_only_my_rules_are_listed_with_readable_times(self):
        self.user(user_id=4)
        r, db = await self.call("GET", f"{PHYSICIAN_BASE}/rules")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["rule_id"] for x in r.json()], [1])
        self.assertEqual((r.json()[0]["start_time"], r.json()[0]["end_time"]), ("09:00", "12:00"))
        self.assertEqual([c for c in db.calls if c[1].endswith("physician_schedule_rules")][0][2]["physician_user_id"], "eq.4")

    async def test_creating_a_rule_uses_the_verified_physician_not_the_body(self):
        self.user(user_id=4)
        r, db = await self.call("POST", f"{PHYSICIAN_BASE}/rules", RULE)
        self.assertEqual((r.status_code, r.json()), (201, {"id": 9}))
        name, payload = db.rpc_calls[0]
        self.assertEqual(name, "upsert_schedule_rule")
        self.assertEqual(payload, {
            "p_actor_user_id": 4, "p_rule_id": None, "p_weekday": 1, "p_start_minute": 540, "p_end_minute": 720,
            "p_slot_minutes": 30, "p_timezone": "America/New_York", "p_effective_from": None, "p_effective_to": None})
        for extra in ({"physician_user_id": 5}, {"p_actor_user_id": 5}, {"is_active": True}):
            r, db = await self.call("POST", f"{PHYSICIAN_BASE}/rules", {**RULE, **extra})
            self.assertEqual(r.status_code, 422, extra)
            self.assertEqual(db.rpc_calls, [], extra)

    async def test_invalid_rules_are_rejected_before_the_database(self):
        self.user()
        bad = [
            {"weekday": 7}, {"weekday": -1}, {"start_time": "9:00"}, {"start_time": "25:00"},
            {"end_time": "09:00"}, {"end_time": "08:00"}, {"slot_minutes": 5}, {"slot_minutes": 121},
            {"slot_minutes": 45},                                # 180 minutes is not a multiple of 45? it is -> use 50
            {"timezone": "Mars/PhysicianBase"}, {"timezone": ""},
            {"effective_from": "2030-02-01", "effective_to": "2030-01-01"},
        ]
        bad[8] = {"slot_minutes": 50}                            # 180 % 50 != 0
        for patch_body in bad:
            r, db = await self.call("POST", f"{PHYSICIAN_BASE}/rules", {**RULE, **patch_body})
            self.assertEqual(r.status_code, 422, patch_body)
            self.assertEqual(db.rpc_calls, [], patch_body)

    async def test_editing_passes_the_rule_id_and_ownership_is_enforced_by_the_database(self):
        self.user(user_id=4)
        r, db = await self.call("POST", f"{PHYSICIAN_BASE}/rules", {**RULE, "rule_id": 1})
        self.assertEqual((r.status_code, db.rpc_calls[0][1]["p_rule_id"]), (201, 1))
        db = make_physician_db()
        db.rpc["upsert_schedule_rule"] = lambda p: httpx.Response(
            404, json={"code": "PT404", "message": "x", "details": "not_found"})
        r, _ = await self.call("POST", f"{PHYSICIAN_BASE}/rules", {**RULE, "rule_id": 2}, db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (404, "not_found"))

    async def test_database_conflicts_map_to_safe_errors(self):
        self.user()
        for reason, state, status in (("rule_overlap", "PT409", 409), ("timezone_mismatch", "PT422", 422),
                                      ("permission_missing", "PT403", 403)):
            db = make_physician_db()
            db.rpc["upsert_schedule_rule"] = lambda p, reason=reason, state=state: httpx.Response(
                400, json={"code": state, "message": "internal sql", "details": reason, "hint": "secret"})
            r, _ = await self.call("POST", f"{PHYSICIAN_BASE}/rules", RULE, db=db)
            self.assertEqual((r.status_code, r.json()["code"]), (status, reason))
            self.assertNotIn("internal sql", r.text)

    async def test_deactivate_uses_the_verified_physician(self):
        self.user(user_id=4)
        r, db = await self.call("POST", f"{PHYSICIAN_BASE}/rules/1/deactivate")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.rpc_calls[0], ("deactivate_schedule_rule", {"p_actor_user_id": 4, "p_rule_id": 1}))


class ExceptionTests(PhysicianBase):
    async def test_only_my_active_exceptions_are_listed(self):
        self.user(user_id=4)
        r, _ = await self.call("GET", f"{PHYSICIAN_BASE}/exceptions")
        self.assertEqual([x["exception_id"] for x in r.json()], [1])
        self.assertEqual(r.json()[0]["starts_at"], "2030-06-03T13:00:00Z")

    async def test_add_unavailable_and_extra_availability(self):
        self.user(user_id=4)
        r, db = await self.call("POST", f"{PHYSICIAN_BASE}/exceptions", EXC)
        self.assertEqual((r.status_code, r.json()), (201, {"id": 9}))
        self.assertEqual(db.rpc_calls[0][1], {
            "p_actor_user_id": 4, "p_kind": "unavailable", "p_starts_at": "2030-06-03T13:00:00Z",
            "p_ends_at": "2030-06-03T17:00:00Z", "p_slot_minutes": None, "p_reason": "Conference"})
        r, db = await self.call("POST", f"{PHYSICIAN_BASE}/exceptions", {
            "kind": "extra_availability", "starts_at": "2030-06-08T14:00:00Z", "ends_at": "2030-06-08T16:00:00Z",
            "slot_minutes": 30})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(db.rpc_calls[0][1]["p_slot_minutes"], 30)

    async def test_invalid_exceptions_are_rejected_before_the_database(self):
        self.user()
        bad = [
            {"ends_at": "2030-06-03T12:00:00Z"},                                   # ends before it starts
            {"starts_at": "2030-06-03T13:00:00"},                                  # no time zone
            {"starts_at": 1900000000},                                             # number, not ISO text
            {"ends_at": "2030-08-03T17:00:00Z"},                                   # longer than 31 days
            {"kind": "extra_availability"},                                        # needs slot_minutes
            {"slot_minutes": 30},                                                  # unavailable can't have slot_minutes
            {"kind": "holiday"}, {"reason": "r" * 201},
        ]
        for patch_body in bad:
            r, db = await self.call("POST", f"{PHYSICIAN_BASE}/exceptions", {**EXC, **patch_body})
            self.assertEqual(r.status_code, 422, patch_body)
            self.assertEqual(db.rpc_calls, [], patch_body)

    async def test_remove_uses_the_verified_physician(self):
        self.user(user_id=4)
        r, db = await self.call("POST", f"{PHYSICIAN_BASE}/exceptions/1/remove")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.rpc_calls[0], ("remove_schedule_exception", {"p_actor_user_id": 4, "p_exception_id": 1}))


class AppointmentViewTests(PhysicianBase):
    async def test_a_physician_sees_only_their_own_appointments_without_free_text(self):
        self.user(user_id=4)
        r, db = await self.call("GET", f"{PHYSICIAN_BASE}/appointments")
        self.assertEqual(r.status_code, 200)
        items = r.json()
        self.assertEqual([i["appointment_id"] for i in items], [1])
        self.assertEqual(items[0]["patient_name"], "Fictional Patient A")
        self.assertEqual(items[0]["local_label"], "Mon, 03 Jun 2030, 10:00 AM EDT")
        for forbidden in ("PRIVATE REASON", "reason", "medical", "treatment", "age"):
            self.assertNotIn(forbidden, r.text)
        query = [c for c in db.calls if c[1].endswith("/appointments")][0][2]
        self.assertEqual(query["physician_user_id"], "eq.4")

    async def test_another_physician_sees_a_different_set(self):
        self.user(user_id=5)
        r, _ = await self.call("GET", f"{PHYSICIAN_BASE}/appointments")
        self.assertEqual([i["appointment_id"] for i in r.json()], [2])


URL = "/api/technician/tasks"


APPT_TASK = 20


def appointment_row(task_id=APPT_TASK, status="Pending Confirmation", physician=4):
    return {"appointment_id": 120, "task_id": task_id, "physician_user_id": physician,
            "starts_at": "2030-06-03T14:00:00+00:00", "ends_at": "2030-06-03T14:30:00+00:00",
            "physician_timezone": "America/New_York", "reason": "Annual check-up", "status": status}


def appt_db(task_status="In Review", **task_extra):
    db = make_tech_db()
    db.tables["technician_tasks"] += [
        make_tech_task(APPT_TASK, status=task_status, category="appointment_scheduling", **task_extra),
        make_tech_task(21, status="In Review", category="appointment_scheduling", assigned_to=7),  # another technician's
    ]
    db.tables["appointments"] = [appointment_row(), appointment_row(21)]
    db.tables["appointments"][1]["appointment_id"] = 121
    db.tables["users"] += [{"user_id": 4, "name": "Dr. Fictional"}]
    db.tables["technician_task_events"] += [
        {"event_id": 10, "task_id": APPT_TASK, "actor_user_id": 2, "actor_role": "Patient", "event_type": "created",
         "created_at": "2026-01-01T00:00:00Z"},
        {"event_id": 11, "task_id": APPT_TASK, "actor_user_id": 6, "actor_role": "Technician", "event_type": "decision",
         "decision": "HOLD", "hold_reason": "needs_information", "internal_note": "INTERNAL ONLY",
         "patient_message": "What is the reason?", "created_at": "2026-01-01T01:00:00Z"},
        {"event_id": 12, "task_id": APPT_TASK, "actor_user_id": 2, "actor_role": "Patient", "event_type": "info_provided",
         "requester_note": "It is my yearly check-up.", "created_at": "2026-01-01T02:00:00Z"},
    ]
    db.rpc["record_technician_task_decision"] = lambda p: {
        "task_id": p["p_task_id"], "status": {"ALLOW": "In Progress", "BLOCK": "Declined", "HOLD": "On Hold"}[p["p_decision"]],
        "latest_decision": p["p_decision"], "outcome": "executed" if p["p_decision"] == "ALLOW" else "recorded",
        "approval_request_id": None, "approval_status": None}
    db.rpc["check_appointment_slot"] = lambda p: {"task_id": p["p_task_id"], "result": "ok", "bookable": True}
    return db


class TechnicianBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env = patch.dict(os.environ, {"HASF_SERVICE_REQUESTS_ENABLED": "true", "HASF_APPOINTMENTS_ENABLED": "true"})
        self._env.start()

    async def asyncTearDown(self):
        self._env.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def user(self, role=Role.TECHNICIAN, perms=TECH_PERMS, user_id=6):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id, email="t@example.test", role=role, permissions=list(perms))

    async def call(self, method, path, body=None, db=None):
        db = db or appt_db()
        with patch("app.main._service_role_key", return_value="k"), \
                patch("app.main._request_supabase", new=db.request):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
                return await c.request(method, path, json=body), db


class DetailTests(TechnicianBase):
    async def test_appointment_detail_includes_doctor_slot_reason_and_patient_response(self):
        self.user()
        r, _ = await self.call("GET", f"{URL}/{APPT_TASK}")
        self.assertEqual(r.status_code, 200)
        apt = r.json()["appointment"]
        self.assertEqual(apt["physician_name"], "Dr. Fictional")
        self.assertEqual(apt["local_label"], "Mon, 03 Jun 2030, 10:00 AM EDT")
        self.assertEqual((apt["reason"], apt["patient_response"]), ("Annual check-up", "It is my yearly check-up."))
        self.assertEqual(apt["status_label"], "Pending Technician Confirmation")
        # Internal notes remain visible to the assigned Technician only, in the existing events list.
        self.assertIn("INTERNAL ONLY", r.text)

    async def test_ordinary_tasks_have_no_appointment_block(self):
        self.user()
        r, db = await self.call("GET", f"{URL}/1")
        self.assertIsNone(r.json()["appointment"])
        self.assertNotIn("appointments", db.read_tables())

    async def test_another_technicians_appointment_is_404(self):
        self.user()
        r, _ = await self.call("GET", f"{URL}/21")
        self.assertEqual(r.status_code, 404)


class FeatureOffAndLegacyTests(TechnicianBase):
    """After migration 013 is applied but BEFORE the feature is switched on, nothing may change."""

    async def test_feature_off_appointment_tasks_behave_like_any_other_task(self):
        self.user()
        with patch.dict(os.environ, {"HASF_APPOINTMENTS_ENABLED": ""}):
            r, db = await self.call("GET", f"{URL}/{APPT_TASK}")
            self.assertEqual(r.status_code, 200)
            self.assertIsNone(r.json()["appointment"])
            self.assertNotIn("appointments", db.read_tables())          # table is not even queried
            # The generic rules apply: a short rejection message is accepted, no template is invented.
            r, db = await self.call("POST", f"{URL}/{APPT_TASK}/decision",
                                    {"decision": "BLOCK", "internal_note": "n", "patient_message": "No."})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("appointments", db.read_tables())
            self.assertNotIn("appointment", r.json()["explanation"].lower())
            r, db = await self.call("POST", f"{URL}/{APPT_TASK}/decision", {"decision": "ALLOW"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(db.rpc_calls[-1][1]["p_patient_message"],
                             "Your request has been accepted and is being handled by your care team.")

    async def test_legacy_appointment_task_without_an_appointments_row_keeps_generic_rules(self):
        self.user()
        db = appt_db()
        db.tables["appointments"] = []                                  # created before migration 013
        r, db = await self.call("GET", f"{URL}/{APPT_TASK}", db=db)
        self.assertIsNone(r.json()["appointment"])
        r, db = await self.call("POST", f"{URL}/{APPT_TASK}/decision",
                                {"decision": "BLOCK", "internal_note": "n", "patient_message": "No."}, db=db)
        self.assertEqual(r.status_code, 200)                           # generic rules: no 20-character minimum


class DecisionMappingTests(TechnicianBase):
    async def decide(self, body, db=None):
        self.user()
        return await self.call("POST", f"{URL}/{APPT_TASK}/decision", body, db=db)

    async def test_confirm_maps_to_allow_with_a_generated_confirmation_message(self):
        r, db = await self.decide({"decision": "ALLOW"})
        self.assertEqual(r.status_code, 200, r.text)
        name, payload = db.rpc_calls[-1]
        self.assertEqual((name, payload["p_decision"], payload["p_guardrail_decision"]),
                         ("record_technician_task_decision", "ALLOW", "Allow"))
        self.assertEqual(payload["p_patient_message"],
                         "Your appointment with Dr. Fictional on Mon, 03 Jun 2030, 10:00 AM EDT is confirmed.")
        self.assertIn("slot is now reserved", r.json()["explanation"])
        self.assertEqual(r.json()["status"], "In Progress")

    async def test_confirm_uses_the_technicians_own_message_when_given(self):
        r, db = await self.decide({"decision": "ALLOW", "patient_message": "See you at 10, please arrive early."})
        self.assertEqual(db.rpc_calls[-1][1]["p_patient_message"], "See you at 10, please arrive early.")

    async def test_too_short_texts_are_refused_before_the_database(self):
        cases = [
            ({"decision": "ALLOW", "patient_message": "ok"}, "confirmation_message_required"),
            ({"decision": "BLOCK", "internal_note": "n", "patient_message": "No."}, "rejection_reason_too_short"),
            ({"decision": "BLOCK", "internal_note": "n", "patient_message": "x" * 19}, "rejection_reason_too_short"),
            ({"decision": "HOLD", "hold_reason": "needs_information", "internal_note": "n", "patient_message": "Why?"},
             "information_request_too_short"),
        ]
        for body, code in cases:
            r, db = await self.decide(body, db=appt_db())
            self.assertEqual((r.status_code, r.json()["code"]), (422, code), body)
            self.assertEqual([c for c in db.rpc_calls if c[0] == "record_technician_task_decision"], [], body)

    async def test_reject_and_request_information_map_to_block_and_hold(self):
        r, db = await self.decide({"decision": "BLOCK", "internal_note": "Not eligible",
                                   "patient_message": "We cannot offer that time; please pick another slot."})
        self.assertEqual((r.status_code, db.rpc_calls[-1][1]["p_decision"], r.json()["status"]), (200, "BLOCK", "Declined"))
        self.assertIn("patient can see your explanation", r.json()["explanation"])
        r, db = await self.decide({"decision": "HOLD", "hold_reason": "needs_information", "internal_note": "Need reason",
                                   "patient_message": "Please tell us the reason for your visit."})
        self.assertEqual((r.status_code, db.rpc_calls[-1][1]["p_hold_reason"], r.json()["status"]),
                         (200, "needs_information", "On Hold"))
        self.assertIn("Review resumes when they reply", r.json()["explanation"])

    async def test_slot_conflict_is_a_safe_409_and_is_audited_with_ids_only(self):
        db = appt_db()
        db.rpc["record_technician_task_decision"] = lambda p: httpx.Response(
            400, json={"code": "PT409", "message": "sql text", "details": "slot_unavailable", "hint": "secret"})
        r, db = await self.decide({"decision": "ALLOW"}, db=db)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "slot_unavailable"))
        self.assertNotIn("sql text", r.text)
        self.assertNotIn("secret", r.text)
        audit = [a for a in db.audits() if a.get("event_type") == "technician.appointment.confirm_conflict"]
        self.assertEqual(len(audit), 1)

    async def test_ordinary_tasks_keep_the_generic_behaviour(self):
        self.user()
        db = appt_db()
        db.tables["technician_tasks"][0]["status"] = "In Review"
        r, db = await self.call("POST", f"{URL}/1/decision",
                                {"decision": "BLOCK", "internal_note": "n", "patient_message": "No."}, db=db)
        self.assertEqual(r.status_code, 200)                       # short text is fine for non-appointments
        self.assertNotIn("appointment", r.json()["explanation"].lower())

    async def test_other_technicians_appointment_cannot_be_decided(self):
        self.user()
        r, db = await self.call("POST", f"{URL}/21/decision", {"decision": "ALLOW"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual([c for c in db.rpc_calls if c[0] == "record_technician_task_decision"], [])

    async def test_technician_without_update_permission_is_refused(self):
        self.user(perms=["view_assigned_task"])
        r, db = await self.call("POST", f"{URL}/{APPT_TASK}/decision", {"decision": "ALLOW"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(db.rpc_calls, [])


class SlotCheckTests(TechnicianBase):
    async def test_preflight_reports_availability_and_is_read_only(self):
        self.user()
        r, db = await self.call("GET", f"{URL}/{APPT_TASK}/appointment-check")
        self.assertEqual((r.status_code, r.json()["result"], r.json()["bookable"]), (200, "ok", True))
        self.assertEqual(db.rpc_calls, [("check_appointment_slot", {"p_task_id": APPT_TASK, "p_actor_user_id": 6})])
        self.assertEqual([c for c in db.calls if c[0] == "POST" and "/rpc/" not in c[1] and "audit" not in c[1]], [])

    async def test_preflight_conflict_message_is_actionable(self):
        self.user()
        db = appt_db()
        db.rpc["check_appointment_slot"] = lambda p: {"task_id": p["p_task_id"], "result": "slot_unavailable", "bookable": False}
        r, _ = await self.call("GET", f"{URL}/{APPT_TASK}/appointment-check", db=db)
        self.assertFalse(r.json()["bookable"])
        self.assertIn("Reject the request", r.json()["message"])

    async def test_preflight_unknown_result_is_not_trusted(self):
        self.user()
        db = appt_db()
        db.rpc["check_appointment_slot"] = lambda p: {"task_id": p["p_task_id"], "result": "surprise", "bookable": True}
        r, _ = await self.call("GET", f"{URL}/{APPT_TASK}/appointment-check", db=db)
        self.assertEqual(r.status_code, 502)

    async def test_preflight_only_for_own_appointment_tasks(self):
        self.user()
        for task_id in (1, 21, 999):                                # generic task, other technician's, unknown
            r, db = await self.call("GET", f"{URL}/{task_id}/appointment-check")
            self.assertEqual(r.status_code, 404, task_id)
            self.assertEqual(db.rpc_calls, [], task_id)


if __name__ == "__main__":
    unittest.main()
