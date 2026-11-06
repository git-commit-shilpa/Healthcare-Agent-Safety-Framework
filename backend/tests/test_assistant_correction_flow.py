import logging
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app import chat_state
from app.chat_state import issue_state
from app.intent_recognizer import Recognition
from app.main import CurrentUser, Role, app, get_current_user

ALL = ["request_ai_assistance", "view_own_record", "view_own_correction_requests", "submit_record_correction"]
PROPOSAL = "Fictional replacement schedule SECRET-PROPOSAL"
OK = {"request_id": 9, "guardrail_decision": "Escalate", "approval_status": "Pending"}


def user(uid=2, session="sess-A", permissions=None, role=Role.PATIENT):
    current = CurrentUser(user_id=uid, email="p@example.test", role=role, permissions=ALL if permissions is None else permissions)
    current._session_key = session
    return current


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, path, body, current=None, blocking=None, rpc=None):
        app.dependency_overrides[get_current_user] = lambda: current or user()
        rec = AsyncMock(return_value=Recognition('{"intent": "request_correction", "confidence": 0.9}', True))
        self.rpc = rpc or AsyncMock(return_value=httpx.Response(200, json=OK))
        self.rec = rec
        try:
            with patch("app.main._blocking_correction_request_id", AsyncMock(return_value=blocking)) as pending, \
                 patch("app.main.recognize_intent", rec), \
                 patch("app.main._write_chat_audit", AsyncMock()), \
                 patch("app.patient_chat.generate_reply", AsyncMock()) as gen, \
                 patch("app.main._service_role_key", return_value="svc"), \
                 patch("app.main._request_supabase", self.rpc):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post(path, json=body)
        finally:
            app.dependency_overrides.clear()
        self.pending, self.gen = pending, gen
        return response

    async def chat(self, message, token=None, **kw):
        body = {"message": message, **({"state_token": token} if token else {})}
        return await self.call("/api/patient/assistant/chat", body, **kw)

    async def to_details(self, **kw):
        offer = (await self.chat("correct my record")).json()["state_token"]
        return (await self.chat("yes", offer, **kw)).json()["state_token"]

    async def to_confirm(self, text=PROPOSAL):
        details = await self.to_details()
        response = await self.call("/api/patient/assistant/correction/review",
                                   {"proposed_treatment": text, "state_token": details})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def confirm(self, token, text=PROPOSAL, **kw):
        return await self.call("/api/patient/assistant/correction/confirm",
                               {"proposed_treatment": text, "state_token": token}, **kw)

    async def test_full_flow_submits_once_through_the_shared_rpc(self):
        review = await self.to_confirm()
        self.assertEqual(review["proposed_treatment"], PROPOSAL)
        self.assertIn("does not change your record", review["notice"])
        response = await self.confirm(review["state_token"])
        self.assertEqual((response.status_code, response.json()["request_id"]), (200, 9))
        self.rpc.assert_awaited_once()
        sent = self.rpc.await_args.kwargs
        self.assertIn("submit_patient_treatment_correction", self.rpc.await_args.args[0])
        self.assertEqual(sent["json_body"], {"p_requested_by": 2, "p_proposed_treatment": PROPOSAL})

    async def test_conversational_yes_and_review_never_submit_or_reach_a_model(self):
        details = await self.to_details()
        self.rpc.assert_not_awaited()
        review = await self.call("/api/patient/assistant/correction/review",
                                 {"proposed_treatment": PROPOSAL, "state_token": details})
        self.rpc.assert_not_awaited()
        self.gen.assert_not_awaited()
        reply = (await self.chat("yes", review.json()["state_token"])).json()
        self.assertEqual(reply["mode"], "notice")
        self.assertIn("press Confirm", reply["reply"])
        self.rpc.assert_not_awaited()

    async def test_chat_cancel_at_review_invalidates_confirm(self):
        review = await self.to_confirm()
        reply = (await self.chat("cancel", review["state_token"])).json()
        self.assertEqual(reply["mode"], "cancelled")
        self.assertEqual((await self.confirm(review["state_token"])).status_code, 409)
        self.rpc.assert_not_awaited()

    async def test_confirm_requires_the_exact_reviewed_text_and_a_confirm_token(self):
        review = await self.to_confirm()
        self.assertEqual((await self.confirm(review["state_token"], text=PROPOSAL + " edited")).status_code, 409)
        details = await self.to_details()
        self.assertEqual((await self.confirm(details)).status_code, 409)  # details token can't confirm
        offered = issue_state(2, "offered", session_key="sess-A")
        self.assertEqual((await self.confirm(offered)).status_code, 409)
        self.rpc.assert_not_awaited()

    async def test_replayed_confirm_does_not_submit_twice(self):
        review = await self.to_confirm()
        rpc = AsyncMock(return_value=httpx.Response(200, json=OK))
        self.assertEqual((await self.confirm(review["state_token"], rpc=rpc)).status_code, 200)
        self.assertEqual((await self.confirm(review["state_token"], rpc=rpc)).status_code, 409)
        rpc.assert_awaited_once()

    async def test_review_token_is_single_use_and_forged_or_expired_tokens_fail(self):
        details = await self.to_details()
        body = {"proposed_treatment": PROPOSAL, "state_token": details}
        self.assertEqual((await self.call("/api/patient/assistant/correction/review", body)).status_code, 200)
        self.assertEqual((await self.call("/api/patient/assistant/correction/review", body)).status_code, 409)
        for token in (details[:-3] + "AAA", "x.y",
                      issue_state(2, "details", now=1000, session_key="sess-A")):
            r = await self.call("/api/patient/assistant/correction/review", {**body, "state_token": token})
            self.assertEqual(r.status_code, 409)

    async def test_cross_user_and_stale_session_are_rejected(self):
        review = await self.to_confirm()
        for current in (user(uid=3), user(session="sess-B")):
            r = await self.confirm(review["state_token"], current=current)
            self.assertEqual(r.status_code, 409)
        details = await self.to_details()
        r = await self.call("/api/patient/assistant/correction/review",
                            {"proposed_treatment": PROPOSAL, "state_token": details}, current=user(session="sess-B"))
        self.assertEqual(r.status_code, 409)
        self.rpc.assert_not_awaited()
        # The legitimate holder can still confirm: failed attempts did not consume the token.
        self.assertEqual((await self.confirm(review["state_token"])).status_code, 200)

    async def test_missing_login_session_fails_closed(self):
        details = issue_state(2, "details")
        r = await self.call("/api/patient/assistant/correction/review",
                            {"proposed_treatment": PROPOSAL, "state_token": details}, current=user(session=None))
        self.assertEqual(r.status_code, 409)

    async def test_roles_permissions_and_extra_fields_are_enforced(self):
        token = issue_state(2, "confirm", session_key="sess-A", proposal_hash=chat_state.bind_text(PROPOSAL))
        for current in (user(role=Role.ADMIN), user(role=Role.PHYSICIAN), user(role=Role.TECHNICIAN),
                        user(permissions=["request_ai_assistance"]), user(permissions=["submit_record_correction"])):
            self.assertEqual((await self.confirm(token, current=current)).status_code, 403)
        for extra in ({"patient_id": 101}, {"reviewer_id": 4}, {"user_id": 9}, {"decision": "Allow"}):
            r = await self.call("/api/patient/assistant/correction/confirm",
                                {"proposed_treatment": PROPOSAL, "state_token": token, **extra})
            self.assertEqual(r.status_code, 422)
        for text in ("", "   ", "x" * 2001):
            self.assertEqual((await self.confirm(token, text=text)).status_code, 422)
        self.rpc.assert_not_awaited()

    async def test_pending_request_blocks_review_and_confirm_rechecked_at_submit(self):
        details = await self.to_details()
        r = await self.call("/api/patient/assistant/correction/review",
                            {"proposed_treatment": PROPOSAL, "state_token": details}, blocking=5)
        self.assertEqual(r.status_code, 409)
        self.assertIn("#5", r.json()["detail"])
        review = await self.to_confirm()
        r = await self.confirm(review["state_token"], blocking=5)  # became pending after the review
        self.assertEqual(r.status_code, 409)
        self.rpc.assert_not_awaited()

    async def test_database_duplicate_guard_is_preserved(self):
        review = await self.to_confirm()
        rpc = AsyncMock(return_value=httpx.Response(409, json={"code": "PT409", "message": "pending"}))
        r = await self.confirm(review["state_token"], rpc=rpc)
        self.assertEqual(r.status_code, 409)
        self.assertIn("already awaiting Physician review", r.json()["detail"])

    async def test_timeout_is_unconfirmed_not_retried_and_token_is_spent(self):
        review = await self.to_confirm()
        request = httpx.Request("POST", "http://svc/rpc")
        failure = HTTPException(status_code=503, detail="x")
        failure.__cause__ = httpx.ReadTimeout("slow", request=request)
        rpc = AsyncMock(side_effect=failure)
        r = await self.confirm(review["state_token"], rpc=rpc)
        self.assertEqual(r.status_code, 504)
        self.assertEqual(rpc.await_count, 1)
        self.assertEqual((await self.confirm(review["state_token"], rpc=rpc)).status_code, 409)
        self.assertEqual(rpc.await_count, 1)

    async def test_submission_failures_map_as_before_and_do_not_retry(self):
        for status, body, expected in ((500, {"code": "XX000", "message": "boom"}, 503), (403, {"code": "42501", "message": "no"}, 403)):
            review = await self.to_confirm()
            rpc = AsyncMock(return_value=httpx.Response(status, json=body))
            r = await self.confirm(review["state_token"], rpc=rpc)
            self.assertEqual(r.status_code, expected)
            self.assertEqual(rpc.await_count, 1)

    async def test_proposal_text_is_never_logged(self):
        with self.assertLogs(level=logging.DEBUG) as captured:
            review = await self.to_confirm()
            await self.confirm(review["state_token"])
            logging.getLogger("probe").debug("probe")
        self.assertNotIn("SECRET-PROPOSAL", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
