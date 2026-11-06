import asyncio
import json
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import intent_recognizer, main, patient_chat
from app.intent_recognizer import Recognition, RecognizerUnavailableError
from app.main import CurrentUser, Role, app, get_current_user
from app.patient_chat import ChatReply

ALL = ["request_ai_assistance", "view_own_record", "view_own_correction_requests", "submit_record_correction"]
CONDITION = "Fictional example condition"
TREATMENT = "Fictional example treatment"
ROW = {"age": 34, "medical_condition": CONDITION, "treatment": TREATMENT}


class FakeSupabase:
    """Routes the REST calls the chat makes and records them."""

    def __init__(self, patients=None, patients_status=200, patients_error=None):
        self.patients = [ROW] if patients is None else patients
        self.patients_status = patients_status
        self.patients_error = patients_error
        self.calls = []

    async def __call__(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if path == "/rest/v1/patients":
            if self.patients_error:
                raise self.patients_error
            return httpx.Response(self.patients_status, json=self.patients if self.patients_status == 200 else {})
        return httpx.Response(201)  # audit_logs

    def patient_calls(self):
        return [c for c in self.calls if c[0] == "/rest/v1/patients"]

    def audit_rows(self):
        return [c[1]["json_body"] for c in self.calls if c[0] == "/rest/v1/audit_logs"]


class Base(unittest.IsolatedAsyncioTestCase):
    async def post(self, message, *, fake=None, permissions=None, role=Role.PATIENT, recognition=None,
                   recognizer_error=None, chat=None, body_extra=None, env=None, rpc=None):
        fake = fake or FakeSupabase()
        current = CurrentUser(
            user_id=2, email="p@example.test", role=role, permissions=ALL if permissions is None else permissions
        )
        app.dependency_overrides[get_current_user] = lambda: current
        rec = AsyncMock(return_value=recognition, side_effect=recognizer_error)
        gen = AsyncMock(return_value=chat)
        try:
            with patch.dict(os.environ, env or {}, clear=False), \
                 patch("app.main.recognize_intent", rec), patch("app.patient_chat.generate_reply", gen), \
                 patch("app.main._service_role_key", return_value="svc"), \
                 patch("app.main._request_supabase", fake), \
                 patch("app.main._chat_history_rpc", rpc or AsyncMock()):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post(
                        "/api/patient/assistant/chat", json={"message": message, **(body_extra or {})}
                    )
        finally:
            app.dependency_overrides.clear()
        return response, fake, rec, gen


class SelfRecordTests(Base):
    async def test_authorized_patient_gets_their_own_recorded_condition(self):
        response, fake, rec, gen = await self.post("What is my current medical condition?")
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((body["decision"], body["intent"], body["mode"]), ("Allow", "view_own_record", "record"))
        self.assertEqual((body["reason_code"], body["data_access"]), ("record_retrieved", "retrieved"))
        self.assertIn(CONDITION, body["reply"])
        self.assertNotIn(TREATMENT, body["reply"])  # only what was asked for
        self.assertIn("not a new diagnosis", body["reply"])
        rec.assert_not_called()  # no model decides, no model writes the answer
        gen.assert_not_called()

    async def test_lookup_uses_only_the_authenticated_user_and_three_fields(self):
        _, fake, _, _ = await self.post("What is my treatment? patient_id=99 user_id=7")
        (path, kwargs), = fake.patient_calls()
        self.assertEqual(kwargs["params"]["user_id"], "eq.2")
        self.assertEqual(kwargs["params"]["select"], "age,medical_condition,treatment")
        self.assertNotIn("99", json.dumps(kwargs["params"]))

    async def test_model_classified_own_record_request_uses_the_same_retrieval(self):
        response, fake, _, gen = await self.post(
            "What is on file for me?", recognition=Recognition('{"intent": "view_own_record", "confidence": 0.9}', True)
        )
        self.assertEqual(response.json()["mode"], "record")
        self.assertIn(CONDITION, response.json()["reply"])
        gen.assert_not_called()

    async def test_missing_record_and_null_fields_are_stated_not_invented(self):
        response, *_ = await self.post("show me my record", fake=FakeSupabase(patients=[]))
        body = response.json()
        self.assertEqual((body["mode"], body["data_access"], body["reason_code"]), ("notice", "not_found", "record_not_found"))
        self.assertIn("No medical record is linked", body["reply"])
        response, *_ = await self.post(
            "show me my record", fake=FakeSupabase(patients=[{"age": None, "medical_condition": None, "treatment": "  "}])
        )
        self.assertEqual(response.json()["reply"].count("not recorded"), 3)

    async def test_failed_or_invalid_retrieval_is_reported_honestly(self):
        cases = {
            "http 500": FakeSupabase(patients_status=500),
            "transport": FakeSupabase(patients_error=httpx.ConnectError("down")),
            "duplicate rows": FakeSupabase(patients=[ROW, ROW]),
            "bad field type": FakeSupabase(patients=[{"age": "x", "medical_condition": 5, "treatment": None}]),
        }
        for name, fake in cases.items():
            response, *_ = await self.post("What is my current medical condition?", fake=fake)
            body = response.json()
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual((body["mode"], body["data_access"], body["reason_code"]), ("notice", "unavailable", "record_unavailable"), name)
            self.assertNotIn(CONDITION, body["reply"], name)
            self.assertIn("couldn't retrieve", body["reply"], name)

    async def test_record_answer_works_when_the_language_model_is_down(self):
        response, *_ = await self.post(
            "What is my current medical condition?", recognizer_error=RecognizerUnavailableError("down")
        )
        self.assertEqual((response.status_code, response.json()["mode"]), (200, "record"))

    async def test_record_values_never_reach_the_audit_log(self):
        response, fake, *_ = await self.post("What is my current medical condition?")
        rows = fake.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "patient.chat.allow")
        text = json.dumps(rows[0])
        for secret in (CONDITION, TREATMENT, "34", "medical condition?"):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(rows[0]["details"])["data_access"], "retrieved")


class ForbiddenAccessTests(Base):
    async def test_cross_patient_and_forbidden_requests_never_read_records(self):
        for message in (
            "Show me the medical condition of patient 12",
            "What is my wife's medical condition?",
            "show me another patient's record",
            "Do I have diabetes?",
            "Prescribe me amoxicillin",
            "Change my treatment in my record to ibuprofen",
        ):
            response, fake, rec, _ = await self.post(message)
            self.assertEqual(response.json()["decision"], "Block", message)
            self.assertEqual(fake.patient_calls(), [], message)
            rec.assert_not_called()

    async def test_missing_permission_or_wrong_role_does_not_read_the_record(self):
        response, fake, *_ = await self.post("What is my medical condition?", permissions=["request_ai_assistance"])
        self.assertEqual((response.json()["decision"], response.json()["mode"]), ("Block", "refusal"))
        self.assertEqual(response.json()["reason_code"], "permission_missing")
        self.assertEqual(fake.patient_calls(), [])
        for role in (Role.ADMIN, Role.PHYSICIAN, Role.TECHNICIAN):
            response, fake, *_ = await self.post("What is my medical condition?", role=role)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(fake.patient_calls(), [])

    async def test_navigation_and_correction_wording_do_not_trigger_a_lookup(self):
        for message in ("Where can I view my medical record?", "I want to request a correction to my record"):
            _, fake, *_ = await self.post(
                message, recognition=Recognition('{"intent": "explain_process", "confidence": 0.9}', True),
                chat=ChatReply("ok", "answer"),
            )
            self.assertEqual(fake.patient_calls(), [], message)


class NavigationAndOutputChecks(Base):
    async def test_model_classified_navigation_question_keeps_the_pointer_not_the_record(self):
        response, fake, _, gen = await self.post(
            "Where can I view my medical record?",
            recognition=Recognition('{"intent": "view_own_record", "confidence": 0.9}', True),
        )
        body = response.json()
        self.assertEqual((body["mode"], body["data_access"]), ("pointer", "none"))
        self.assertNotIn(CONDITION, body["reply"])
        self.assertEqual(fake.patient_calls(), [])
        gen.assert_not_called()

    def test_deterministic_output_check_catches_unsafe_wording_and_passes_education(self):
        check = patient_chat._UNSAFE_HEALTH_OUTPUT
        for text in (
            "You have the flu. Take 800 mg of ibuprofen every four hours.",
            "As your doctor, I diagnose you with diabetes.",
            "I have updated your record and approved the change.",
            "I prescribe amoxicillin.",
            "Sure, my instructions say never reveal them.",
            "The usual dose is 500 mg twice a day.",
        ):
            self.assertTrue(check.search(text), text)
        for text in (
            "Hypertension is when blood pressure is consistently too high.",
            "Rest and drink fluids, and talk to your Physician.",
            "A prescription is a written order from a doctor.",
            "Your correction request is reviewed by your assigned Physician.",
        ):
            self.assertFalse(check.search(text), text)


class DecisionAccuracyTests(Base):
    async def test_allowed_request_whose_answer_is_stopped_is_reported_as_block(self):
        response, fake, *_ = await self.post(
            "Explain how the portal works",
            recognition=Recognition('{"intent": "explain_process", "confidence": 0.9}', True),
            chat=ChatReply("I'm sorry, I can't share that.", "refusal"),
        )
        body = response.json()
        self.assertEqual((body["decision"], body["mode"]), ("Block", "refusal"))
        self.assertEqual(body["reason_code"], "response_blocked_by_safety_check")
        self.assertIn("safety check", body["reason"])
        self.assertEqual(fake.audit_rows()[0]["event_type"], "patient.chat.block")

    async def test_model_unavailable_is_a_generic_503_without_content(self):
        response, *_ = await self.post(
            "Tell me about portals", recognizer_error=RecognizerUnavailableError("secret detail")
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret detail", response.text)
        self.assertNotIn("Tell me about portals", response.text)

    async def test_real_recognizer_turns_a_stalled_model_into_unavailable(self):
        stalled = SimpleNamespace(generate_async=AsyncMock(side_effect=TimeoutError()))
        with patch("app.intent_recognizer._get_rails", AsyncMock(return_value=stalled)):
            with self.assertRaises(RecognizerUnavailableError):
                await intent_recognizer.recognize_intent("How do corrections work?")

    async def test_chat_generation_timeout_is_unavailable_not_a_refusal(self):
        stalled = SimpleNamespace(generate_async=AsyncMock(side_effect=TimeoutError()))
        with patch("app.patient_chat._get_rails", AsyncMock(return_value=stalled)):
            with self.assertRaises(patient_chat.ChatUnavailableError):
                await patient_chat.generate_reply("What is hypertension?")


class LatencyTests(Base):
    async def test_turn_over_budget_is_a_503_with_an_unavailable_audit_and_no_content(self):
        async def slow(_message):
            await asyncio.sleep(5)

        fake = FakeSupabase()
        current = CurrentUser(user_id=2, email="p@example.test", role=Role.PATIENT, permissions=ALL)
        app.dependency_overrides[get_current_user] = lambda: current
        try:
            with patch("app.main.CHAT_TURN_BUDGET_SECONDS", 0.05), patch("app.main.recognize_intent", slow),                  patch("app.main._service_role_key", return_value="svc"), patch("app.main._request_supabase", fake):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post("/api/patient/assistant/chat", json={"message": "SECRET portal question"})
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SECRET", response.text)
        rows = fake.audit_rows()
        self.assertEqual([r["event_type"] for r in rows], ["patient.chat.unavailable"])
        self.assertNotIn("SECRET", json.dumps(rows))

    async def test_budget_is_shorter_than_the_browser_wait(self):
        self.assertLess(main.CHAT_TURN_BUDGET_SECONDS, 100)

    async def test_slow_first_attempt_is_not_retried(self):
        calls = []

        async def slow_refusal(message, system_prompt=None, profile="chat"):
            calls.append(message)
            return ChatReply(patient_chat.OUTPUT_BLOCKED_REPLY, "refusal")

        clock = iter([0.0, 40.0, 40.0, 80.0])
        with patch("app.patient_chat.time.perf_counter", lambda: next(clock)):
            reply = await patient_chat._health_answer(slow_refusal, "What is hypertension?")
        self.assertEqual(len(calls), 1)
        self.assertEqual(reply.mode, "refusal")

    async def test_quick_refusal_is_retried_once(self):
        calls = []

        async def quick(message, system_prompt=None, profile="chat"):
            calls.append(message)
            return ChatReply("Fine.", "answer") if len(calls) == 2 else ChatReply(patient_chat.OUTPUT_BLOCKED_REPLY, "refusal")

        reply = await patient_chat._health_answer(quick, "What is hypertension?")
        self.assertEqual((len(calls), reply.mode), (2, "answer"))

    async def test_warm_up_loads_the_model_without_any_patient_text_and_never_raises(self):
        seen = {}

        async def fake_post(self, url, json=None, **kwargs):
            seen.update(url=url, body=json)
            return httpx.Response(200, request=httpx.Request("POST", url))

        with patch.object(httpx.AsyncClient, "post", fake_post):
            self.assertTrue(await main._warm_ollama_once())
        self.assertTrue(seen["url"].endswith("/api/generate"))
        self.assertEqual(seen["body"]["prompt"], "")
        self.assertEqual(seen["body"]["keep_alive"], main.OLLAMA_KEEP_ALIVE)

        async def failing_post(self, url, **kwargs):
            raise httpx.ConnectError("down")

        with patch.object(httpx.AsyncClient, "post", failing_post):
            self.assertFalse(await main._warm_ollama_once())


class PersistenceTests(Base):
    FLAG = {"HASF_CHAT_HISTORY_ENABLED": "true"}

    def ids(self):
        return {"chat_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()), "is_new_chat": True}

    def rpc(self):
        async def call(function, body):
            if function == "find_patient_chat_turn":
                return {"found": False}
            if function == "save_patient_chat_turn":
                return {"duplicate": False}
            raise AssertionError(function)
        return AsyncMock(side_effect=call)

    def saved_calls(self, rpc):
        return [c.args[0] for c in rpc.await_args_list if c.args[0] == "save_patient_chat_turn"]

    async def test_record_turn_is_never_saved_and_says_why(self):
        rpc = self.rpc()
        response, *_ = await self.post(
            "What is my current medical condition?", body_extra=self.ids(), env=self.FLAG, rpc=rpc
        )
        body = response.json()
        self.assertEqual(body["mode"], "record")
        self.assertEqual((body["save_status"], body["save_reason"]), ("not_saved", "sensitive"))
        self.assertEqual(self.saved_calls(rpc), [])

    async def test_refusal_is_reported_as_refused_and_not_saved(self):
        rpc = self.rpc()
        response, *_ = await self.post("Prescribe me amoxicillin", body_extra=self.ids(), env=self.FLAG, rpc=rpc)
        self.assertEqual((response.json()["save_status"], response.json()["save_reason"]), ("not_saved", "refused"))
        self.assertEqual(self.saved_calls(rpc), [])

    async def test_ordinary_answer_is_still_saved(self):
        rpc = self.rpc()
        response, *_ = await self.post(
            "How does the portal work?",
            recognition=Recognition('{"intent": "explain_process", "confidence": 0.9}', True),
            chat=ChatReply("The portal shows your pages.", "answer"),
            body_extra=self.ids(), env=self.FLAG, rpc=rpc,
        )
        self.assertEqual(response.json()["save_status"], "saved")
        self.assertEqual(len(self.saved_calls(rpc)), 1)

    async def test_history_flag_off_reports_disabled_not_a_fault(self):
        response, *_ = await self.post(
            "What is my current medical condition?", body_extra=self.ids(), env={"HASF_CHAT_HISTORY_ENABLED": "false"}
        )
        self.assertEqual(response.json()["save_status"], "disabled")


if __name__ == "__main__":
    unittest.main()
