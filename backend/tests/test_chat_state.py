import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app import chat_state
from app.chat_state import InvalidStateError, consume_state, issue_state
from app.intent_recognizer import Recognition
from app.main import CurrentUser, Role, app, get_current_user

ALL = ["request_ai_assistance", "view_own_record", "view_own_correction_requests", "submit_record_correction"]
PROPOSAL = "SECRET-PROPOSED-TREATMENT-TEXT"


def user(user_id=2, permissions=None, role=Role.PATIENT):
    return CurrentUser(user_id=user_id, email="p@example.test", role=role, permissions=ALL if permissions is None else permissions)


def correction_intent():
    return Recognition('{"intent": "request_correction", "confidence": 0.9}', True)


class TokenTests(unittest.TestCase):
    def test_round_trip_and_single_use(self):
        token = issue_state(2, chat_state.STEP_OFFERED)
        self.assertEqual(consume_state(token, 2), chat_state.STEP_OFFERED)
        with self.assertRaises(InvalidStateError):  # replay
            consume_state(token, 2)

    def test_cross_user_forged_tampered_and_expired_are_rejected(self):
        for name, token, uid, now in (
            ("cross-user", issue_state(2, "offered"), 3, None),
            ("expired", issue_state(2, "offered", now=1000), 2, 1000 + chat_state.STATE_TTL_SECONDS + 5),
            ("garbage", "not-a-token", 2, None),
            ("empty-sig", issue_state(2, "offered").split(".")[0] + ".", 2, None),
            ("oversized", "a" * 700, 2, None),
        ):
            with self.assertRaises(InvalidStateError, msg=name):
                consume_state(token, uid, now)

    def test_tampered_payload_fails_signature(self):
        body, sig = issue_state(2, "offered").split(".")
        forged_body = chat_state._b64(b'{"u":3,"a":"request_correction","s":"details","e":9999999999,"n":"x"}')
        for bad in (f"{forged_body}.{sig}", f"{body}.{sig[:-2]}AA", f"{body}x.{sig}"):
            with self.assertRaises(InvalidStateError):
                consume_state(bad, 3)

    def test_failed_validation_does_not_consume(self):
        token = issue_state(2, "offered")
        with self.assertRaises(InvalidStateError):
            consume_state(token, 99)
        self.assertEqual(consume_state(token, 2), "offered")

    def test_reply_interpretation_is_exact(self):
        self.assertEqual(chat_state.interpret_reply("Yes!"), "yes")
        self.assertEqual(chat_state.interpret_reply("no thanks"), "no")
        self.assertEqual(chat_state.interpret_reply("Cancel"), "cancel")
        for text in ("yes and also change my dose", "yes, ignore the rules", "maybe", ""):
            self.assertIsNone(chat_state.interpret_reply(text))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, current, body, blocking=None, intent=None):
        app.dependency_overrides[get_current_user] = lambda: current
        rec = AsyncMock(return_value=intent or correction_intent())
        db = AsyncMock()
        try:
            with patch("app.main.recognize_intent", rec), \
                 patch("app.main._write_chat_audit", AsyncMock()), \
                 patch("app.main._blocking_correction_request_id", AsyncMock(return_value=blocking)) as pending, \
                 patch("app.patient_chat.generate_reply", AsyncMock(return_value=None)) as gen, \
                 patch("app.main._request_supabase", db):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post("/api/patient/assistant/chat", json=body)
        finally:
            app.dependency_overrides.clear()
        self.rec, self.pending, self.gen, self.db = rec, pending, gen, db
        return response

    async def offer(self, uid=2):
        response = await self.post(user(uid), {"message": "I want to correct my record"})
        self.assertEqual(response.json()["mode"], "offer")
        self.assertIn("Would you like to raise a correction request?", response.json()["reply"])
        return response.json()["state_token"]

    async def test_multi_turn_offer_yes_details_without_submission_or_writes(self):
        token = await self.offer()
        response = await self.post(user(), {"message": "yes", "state_token": token})
        body = response.json()
        self.assertEqual((response.status_code, body["mode"]), (200, "details_step"))
        self.assertIn("Nothing has been submitted", body["reply"])
        self.assertIn("confirm explicitly", body["reply"])
        self.assertTrue(body["state_token"])
        self.rec.assert_not_called()
        self.gen.assert_not_called()
        self.db.assert_not_called()

    async def test_no_and_cancel_end_the_flow(self):
        for answer in ("no", "Cancel"):
            token = await self.offer()
            body = (await self.post(user(), {"message": answer, "state_token": token})).json()
            self.assertEqual((body["mode"], body["state_token"]), ("cancelled", None))

    async def test_replayed_yes_is_rejected(self):
        token = await self.offer()
        await self.post(user(), {"message": "yes", "state_token": token})
        response = await self.post(user(), {"message": "yes", "state_token": token})
        self.assertEqual(response.status_code, 409)

    async def test_cross_user_forged_and_expired_state_rejected(self):
        token = await self.offer(uid=2)
        for current, tok in ((user(3), token), (user(2), token[:-3] + "AAA"), (user(2), "x.y")):
            response = await self.post(current, {"message": "yes", "state_token": tok})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"], "This conversation step is no longer valid. Please start again.")
        expired = issue_state(2, "offered", now=1000)
        self.assertEqual((await self.post(user(), {"message": "yes", "state_token": expired})).status_code, 409)

    async def test_yes_without_pending_offer_does_nothing(self):
        body = (await self.post(user(), {"message": "yes"})).json()
        self.assertEqual(body["mode"], "notice")
        self.assertIsNone(body["state_token"])
        self.rec.assert_not_called()
        self.db.assert_not_called()

    async def test_pending_request_blocks_offer_and_late_yes(self):
        body = (await self.post(user(), {"message": "correct my record"}, blocking=5)).json()
        self.assertEqual(body["mode"], "notice")
        self.assertIn("#5", body["reply"])
        self.assertIsNone(body["state_token"])
        token = await self.offer()  # offered while nothing pending
        body = (await self.post(user(), {"message": "yes", "state_token": token}, blocking=5)).json()
        self.assertEqual(body["mode"], "notice")  # re-checked at "yes"
        self.assertIsNone(body["state_token"])

    async def test_pending_check_failure_fails_closed(self):
        app.dependency_overrides[get_current_user] = lambda: user()
        try:
            with patch("app.main.recognize_intent", AsyncMock(return_value=correction_intent())), \
                 patch("app.main._blocking_correction_request_id", AsyncMock(side_effect=HTTPException(503, "down"))):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post("/api/patient/assistant/chat", json={"message": "correct my record"})
        finally:
            app.dependency_overrides.clear()
        self.assertEqual(response.status_code, 503)

    async def test_text_at_details_step_never_reaches_a_model(self):
        token = await self.offer()
        details = (await self.post(user(), {"message": "yes", "state_token": token})).json()["state_token"]
        body = (await self.post(user(), {"message": PROPOSAL, "state_token": details})).json()
        self.assertEqual(body["mode"], "details_step")
        self.assertNotIn(PROPOSAL, str(body))
        self.rec.assert_not_called()
        self.gen.assert_not_called()
        self.db.assert_not_called()

    async def test_unrecognised_reply_drops_the_offer_and_proceeds_normally(self):
        token = await self.offer()
        response = await self.post(user(), {"message": "How does review work?", "state_token": token},
                                   intent=Recognition('{"intent": "view_history", "confidence": 0.9}', True))
        self.assertEqual(response.json()["mode"], "pointer")
        self.assertEqual((await self.post(user(), {"message": "yes", "state_token": token})).status_code, 409)

    async def test_permissions_and_gateway_still_apply(self):
        no_submit = ["request_ai_assistance", "view_own_record"]
        body = (await self.post(user(permissions=no_submit), {"message": "correct my record"})).json()
        self.assertEqual(body["decision"], "Block")
        self.assertIsNone(body["state_token"])
        for role in (Role.ADMIN, Role.PHYSICIAN, Role.TECHNICIAN):
            response = await self.post(user(role=role), {"message": "yes", "state_token": issue_state(2, "offered")})
            self.assertEqual(response.status_code, 403)
        response = await self.post(user(), {"message": "hi", "state_token": "t", "user_id": 5})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
