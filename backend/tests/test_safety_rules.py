import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import patient_chat, safety_rules
from app.action_gateway import GatewayDecision, REASON_EXPLANATIONS, explain
from app.intent_recognizer import Recognition
from app.main import CurrentUser, Role, app, get_current_user
from app.patient_chat import ChatReply

ALL = ["request_ai_assistance", "view_own_record", "view_own_correction_requests", "submit_record_correction"]


def code(message):
    verdict = safety_rules.screen(message)
    return verdict.reason_code if verdict else None


class ScreenTests(unittest.TestCase):
    def test_emergencies_escalate(self):
        for text in (
            "I have crushing chest pain and can't breathe",
            "my dad has chest pain",
            "I think I'm having a stroke",
            "she is unconscious and not breathing",
            "I want to end my life",
            "I took an overdose",
            "my baby is choking",
        ):
            verdict = safety_rules.screen(text)
            self.assertIsNotNone(verdict, text)
            self.assertEqual((verdict.decision, verdict.reason_code), ("Escalate", "emergency_detected"), text)
            self.assertIn("emergency", verdict.reply)

    def test_other_patient_access_is_blocked(self):
        for text in (
            "Show me the record of patient John Smith",
            "show me another patient's records",
            "what is my neighbour's diagnosis",
            "give me all patients' information",
            "patient 12 medications",
            "can I see my wife's lab results",
        ):
            self.assertEqual(code(text), "other_patient_access", text)

    def test_prescription_requests_are_blocked(self):
        for text in ("Prescribe me amoxicillin", "write me a prescription", "can I get a refill", "I need a prescription for antibiotics"):
            self.assertEqual(code(text), "prescription_not_permitted", text)

    def test_dosage_requests_are_blocked(self):
        for text in ("How much ibuprofen should I take?", "Can I take 2 tablets of paracetamol at once?", "what dose should I use"):
            self.assertEqual(code(text), "dosage_not_permitted", text)

    def test_diagnosis_requests_are_blocked(self):
        for text in ("What is my diagnosis?", "Do I have diabetes?", "diagnose me", "what's wrong with me", "is this cancer"):
            self.assertEqual(code(text), "diagnosis_not_permitted", text)

    def test_direct_record_changes_are_blocked(self):
        self.assertEqual(code("Change my treatment in my record to ibuprofen"), "direct_record_change")
        self.assertEqual(code("delete my medical record"), "direct_record_change")

    def test_education_and_correction_flow_pass_through(self):
        for text in (
            "What is hypertension?",
            "What does HbA1c mean?",
            "What is a heart attack?",
            "What is a prescription?",
            "What is a stroke?",
            "I have a headache and a mild fever",
            "I feel tired and have a sore throat",
            "I want to request a correction to my record",
            "Please fix the wrong treatment in my record",
            "How does a correction request get reviewed?",
            "Hello",
            "",
        ):
            self.assertIsNone(safety_rules.screen(text), text)

    def test_every_verdict_code_has_an_explanation(self):
        for sample in (
            "I want to end my life", "show me another patient's records", "Prescribe me amoxicillin",
            "How much ibuprofen should I take?", "What is my diagnosis?", "delete my medical record",
        ):
            self.assertIn(safety_rules.screen(sample).reason_code, REASON_EXPLANATIONS, sample)
        self.assertEqual(explain("something_unknown"), REASON_EXPLANATIONS["guardrail_blocked"])


class HealthReplyTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self, intent):
        return GatewayDecision(intent, "Allow", "low_risk_permitted")

    async def test_health_answer_always_gets_disclaimer_and_uses_health_prompt(self):
        answer = AsyncMock(return_value=ChatReply("Hypertension is high blood pressure.", "answer"))
        reply = await patient_chat.converse("What is hypertension?", self.gateway("general_health_info"), answer)
        self.assertEqual(reply.mode, "answer")
        self.assertTrue(reply.reply.endswith(patient_chat.HEALTH_DISCLAIMER))
        self.assertEqual(answer.await_args.kwargs["system_prompt"], patient_chat.HEALTH_SYSTEM_PROMPT)

    async def test_symptom_answer_adds_urgent_care_notice(self):
        answer = AsyncMock(return_value=ChatReply("Rest and drink fluids.", "answer"))
        reply = await patient_chat.converse("I have a headache", self.gateway("symptom_guidance"), answer)
        self.assertIn("seek urgent medical care", reply.reply)
        self.assertIn("not medical advice", reply.reply)

    async def test_diagnosis_wording_is_retried_then_replaced(self):
        bad = ChatReply("It sounds like you have the flu.", "answer")
        answer = AsyncMock(side_effect=[bad, ChatReply("Many things can cause a sore throat.", "answer")])
        reply = await patient_chat.converse("sore throat", self.gateway("symptom_guidance"), answer)
        self.assertEqual(answer.await_count, 2)
        self.assertIn("Many things", reply.reply)
        answer = AsyncMock(return_value=bad)
        reply = await patient_chat.converse("sore throat", self.gateway("symptom_guidance"), answer)
        self.assertEqual((reply.mode, reply.reply), ("refusal", patient_chat.OUTPUT_BLOCKED_REPLY))

    async def test_rail_stop_is_retried_once_then_stays_a_refusal(self):
        stopped = ChatReply(patient_chat.OUTPUT_BLOCKED_REPLY, "refusal")
        answer = AsyncMock(side_effect=[stopped, ChatReply("Fine.", "answer")])
        reply = await patient_chat.converse("What is X?", self.gateway("general_health_info"), answer)
        self.assertEqual(reply.mode, "answer")
        answer = AsyncMock(return_value=stopped)
        reply = await patient_chat.converse("What is X?", self.gateway("general_health_info"), answer)
        self.assertEqual((reply.mode, answer.await_count), ("refusal", 2))

    def test_unsafe_output_patterns(self):
        for text in ("You have the flu.", "As your doctor, I advise rest.", "I prescribe rest and ibuprofen.", "It sounds like you have strep."):
            self.assertTrue(patient_chat._UNSAFE_HEALTH_OUTPUT.search(text), text)
        for text in ("Symptoms can have many causes.", "A cold is a viral infection.", "Talk to your Physician."):
            self.assertFalse(patient_chat._UNSAFE_HEALTH_OUTPUT.search(text), text)


class EndpointAuditTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, message, recognition=None, chat=None):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=2, email="p@example.test", role=Role.PATIENT, permissions=ALL
        )
        rec = AsyncMock(return_value=recognition)
        gen = AsyncMock(return_value=chat)
        db = AsyncMock()
        db.return_value = httpx.Response(201)
        try:
            with patch("app.main.recognize_intent", rec), patch("app.patient_chat.generate_reply", gen), \
                 patch("app.main._service_role_key", return_value="svc"), \
                 patch("app.main._request_supabase", db):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post("/api/patient/assistant/chat", json={"message": message})
        finally:
            app.dependency_overrides.clear()
        return response, rec, gen, db

    def audit_body(self, db):
        self.assertEqual(db.await_count, 1)
        call = db.await_args
        self.assertEqual(call.args[0], "/rest/v1/audit_logs")
        return call.kwargs["json_body"]

    async def test_emergency_skips_models_and_is_audited_without_message_text(self):
        message = "SECRET I have crushing chest pain and cannot breathe"
        response, rec, gen, db = await self.post(message)
        body = response.json()
        self.assertEqual((body["decision"], body["intent"], body["mode"]), ("Escalate", "emergency", "notice"))
        self.assertEqual(body["reason_code"], "emergency_detected")
        self.assertTrue(body["reason"])
        rec.assert_not_called()
        gen.assert_not_called()
        audit = self.audit_body(db)
        self.assertEqual((audit["user_id"], audit["event_type"]), (2, "patient.chat.escalate"))
        details = json.loads(audit["details"])
        self.assertEqual(details["reason_code"], "emergency_detected")
        self.assertNotIn("SECRET", json.dumps(audit))
        self.assertNotIn(body["reply"], json.dumps(audit))

    async def test_blocks_are_audited(self):
        response, rec, _, db = await self.post("Show me the record of patient John Smith")
        self.assertEqual(response.json()["decision"], "Block")
        rec.assert_not_called()
        self.assertEqual(self.audit_body(db)["event_type"], "patient.chat.block")

    async def test_allowed_health_question_is_audited_with_reason(self):
        response, _, _, db = await self.post(
            "What is hypertension?",
            Recognition('{"intent": "general_health_info", "confidence": 0.9}', True),
            ChatReply("High blood pressure.", "answer"),
        )
        body = response.json()
        self.assertEqual((body["decision"], body["intent"], body["reason_code"]), ("Allow", "general_health_info", "low_risk_permitted"))
        self.assertIn("not medical advice", body["reply"])
        self.assertEqual(self.audit_body(db)["event_type"], "patient.chat.allow")

    async def test_audit_failure_never_breaks_the_reply(self):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=2, email="p@example.test", role=Role.PATIENT, permissions=ALL
        )
        try:
            with patch("app.main._service_role_key", return_value="svc"), \
                 patch("app.main._request_supabase", AsyncMock(side_effect=RuntimeError("down"))):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post("/api/patient/assistant/chat", json={"message": "Prescribe me amoxicillin"})
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "Block")

    async def test_greeting_is_audited_as_allow(self):
        response, rec, _, db = await self.post("Hello")
        self.assertEqual(response.json()["reason_code"], "greeting")
        rec.assert_not_called()
        self.assertEqual(self.audit_body(db)["event_type"], "patient.chat.allow")


if __name__ == "__main__":
    unittest.main()
