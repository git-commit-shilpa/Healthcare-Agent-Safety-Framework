"""Restoring a saved conversation.

Correction-flow turns are stored as a fixed placeholder pair on purpose (the Patient's real
wording is never kept). Reopening a conversation must show a single privacy notice for them,
never a run of fake "Patient: Correction request step" messages, and must never hide or
rewrite the Patient's real messages. All data below is fictional.
"""

import copy
import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx

from app import chat_history
from app.intent_recognizer import Recognition
from app.main import CurrentUser, Role, app, get_current_user
from app.patient_chat import ChatReply

ALL = ["request_ai_assistance", "view_own_record", "view_own_correction_requests", "submit_record_correction"]
CHAT = str(uuid.uuid4())
PH_USER = chat_history.EVENT_USER_TEXT
PH_REPLY = chat_history.EVENT_REPLY_TEXT


def row(role, content, mode=None, n=0):
    return {"role": role, "content": content, "display_mode": mode, "created_at": f"2026-11-05T10:0{n}:00+00:00"}


def user(text, n=0):
    return row("user", text, None, n)


def bot(text, mode="answer", n=0):
    return row("assistant", text, mode, n)


def event_pair(n=0):
    return [user(PH_USER, n), bot(PH_REPLY, "event", n)]


class PresentMessagesTests(unittest.TestCase):
    def test_a_run_of_placeholder_pairs_becomes_one_notice(self):
        rows = event_pair(1) + event_pair(2) + event_pair(3) + event_pair(4) + event_pair(5)
        shown = chat_history.present_messages(rows)
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["role"], "assistant")
        self.assertEqual(shown[0]["display_mode"], "event")
        self.assertEqual(shown[0]["content"], chat_history.REDACTED_NOTICE)
        self.assertNotIn(PH_USER, [m["content"] for m in shown])

    def test_real_messages_around_placeholders_are_kept_in_order_and_unchanged(self):
        first = [user("What does the portal show?", 1), bot("Your pages and request history.", "answer", 1)]
        last = [user("Where is my history?", 8), bot("In AI Request History.", "pointer", 8)]
        shown = chat_history.present_messages(first + event_pair(3) + event_pair(4) + last)
        self.assertEqual(shown[:2], first)
        self.assertEqual(shown[3:], last)
        self.assertEqual(shown[2]["content"], chat_history.REDACTED_NOTICE)
        self.assertEqual(len(shown), 5)

    def test_separate_runs_each_get_their_own_notice(self):
        real = [user("Hello there", 3), bot("Hi!", "greeting", 3)]
        shown = chat_history.present_messages(event_pair(1) + real + event_pair(5))
        self.assertEqual([m["display_mode"] for m in shown], ["event", None, "greeting", "event"])

    def test_a_patient_message_with_the_same_words_is_never_hidden(self):
        typed = [user(PH_USER, 1), bot("I can explain how corrections are reviewed.", "answer", 1)]
        self.assertEqual(chat_history.present_messages(typed), typed)
        alone = [user(PH_USER, 1)]
        self.assertEqual(chat_history.present_messages(alone), alone)

    def test_orphan_event_reply_still_becomes_a_notice_and_nothing_is_invented(self):
        shown = chat_history.present_messages([bot(PH_REPLY, "event", 1)])
        self.assertEqual([m["content"] for m in shown], [chat_history.REDACTED_NOTICE])

    def test_stored_rows_are_not_modified(self):
        rows = event_pair(1) + [user("Hello there", 2), bot("Hi!", "greeting", 2)]
        before = copy.deepcopy(rows)
        chat_history.present_messages(rows)
        self.assertEqual(rows, before)

    def test_empty_and_malformed_input_are_safe(self):
        self.assertEqual(chat_history.present_messages([]), [])
        self.assertEqual(chat_history.present_messages(["odd", None]), ["odd", None])

    def test_the_notice_carries_no_patient_information_and_points_to_request_history(self):
        notice = chat_history.REDACTED_NOTICE
        self.assertIn("AI Request History", notice)
        self.assertIn("never saved", notice)
        self.assertLess(len(notice), 2000)
        self.assertFalse(chat_history._looks_unsafe(notice, state_token=None, user_text=False))


class RestoreEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, method, path, rpc, *, body=None, env=None):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=2, email="p@example.test", role=Role.PATIENT, permissions=ALL
        )
        try:
            with patch.dict(os.environ, env or {"HASF_CHAT_HISTORY_ENABLED": "true"}), \
                 patch("app.main._chat_history_rpc", rpc), \
                 patch("app.main._write_chat_audit", AsyncMock()):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    return await client.request(method, path, json=body)
        finally:
            app.dependency_overrides.clear()

    async def test_reopening_a_mixed_conversation_shows_real_messages_and_one_notice(self):
        stored = (
            [user("What does the portal show?", 1), bot("Your pages and request history.", "answer", 1)]
            + event_pair(2) + event_pair(3) + event_pair(4)
        )
        rpc = AsyncMock(return_value=stored)
        response = await self.request("GET", f"/api/patient/assistant/chats/{CHAT}", rpc)
        self.assertEqual(response.status_code, 200)
        messages = response.json()["messages"]
        self.assertEqual([m["content"] for m in messages][:2], ["What does the portal show?", "Your pages and request history."])
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[2]["content"], chat_history.REDACTED_NOTICE)
        self.assertNotIn(PH_USER, response.text)
        self.assertNotIn(PH_REPLY, response.text)

    async def test_reading_a_conversation_only_reads_with_the_owner_id(self):
        rpc = AsyncMock(return_value=event_pair(1))
        await self.request("GET", f"/api/patient/assistant/chats/{CHAT}", rpc)
        rpc.assert_awaited_once_with("get_patient_chat_messages", {"p_user_id": 2, "p_chat_id": CHAT})

    async def test_an_all_placeholder_conversation_returns_one_notice_not_an_empty_chat(self):
        rpc = AsyncMock(return_value=event_pair(1) + event_pair(2))
        response = await self.request("GET", f"/api/patient/assistant/chats/{CHAT}", rpc)
        self.assertEqual([m["display_mode"] for m in response.json()["messages"]], ["event"])

    async def test_ordinary_conversations_are_returned_exactly_as_stored(self):
        stored = [user("Hello there", 1), bot("Hi!", "greeting", 1), user("What is hypertension?", 2), bot("It is high blood pressure.", "answer", 2)]
        rpc = AsyncMock(return_value=copy.deepcopy(stored))
        response = await self.request("GET", f"/api/patient/assistant/chats/{CHAT}", rpc)
        self.assertEqual(response.json()["messages"], stored)

    async def test_deletion_still_removes_only_the_owners_chat(self):
        rpc = AsyncMock(return_value=True)
        response = await self.request("DELETE", f"/api/patient/assistant/chats/{CHAT}", rpc)
        self.assertEqual(response.status_code, 204)
        rpc.assert_awaited_once_with("delete_patient_chat", {"p_user_id": 2, "p_chat_id": CHAT})


class SaveStatusTests(unittest.IsolatedAsyncioTestCase):
    """A correction step is stored as a placeholder: say so, and never send the Patient's words."""

    SECRET = "FICTIONAL-SECRET-CORRECTION-WORDS"

    async def chat(self, message, recognition, **body):
        calls = []

        async def rpc(function, payload):
            calls.append((function, payload))
            if function == "find_patient_chat_turn":
                return {"found": False}
            if function == "save_patient_chat_turn":
                return {"duplicate": False}
            raise AssertionError(function)

        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=2, email="p@example.test", role=Role.PATIENT, permissions=ALL
        )
        ids = {"chat_id": CHAT, "turn_id": str(uuid.uuid4()), "is_new_chat": True}
        try:
            with patch.dict(os.environ, {"HASF_CHAT_HISTORY_ENABLED": "true"}), \
                 patch("app.main.recognize_intent", AsyncMock(return_value=recognition)), \
                 patch("app.main._chat_history_rpc", rpc), \
                 patch("app.main._write_chat_audit", AsyncMock()), \
                 patch("app.main._blocking_correction_request_id", AsyncMock(return_value=None)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    response = await client.post(
                        "/api/patient/assistant/chat", json={"message": message, **ids, **body}
                    )
        finally:
            app.dependency_overrides.clear()
        return response, calls

    async def test_correction_turn_is_saved_only_as_a_placeholder_and_reported_truthfully(self):
        rec = Recognition('{"intent": "request_correction", "confidence": 0.9}', True)
        response, calls = await self.chat(f"I want to fix my record: {self.SECRET}", rec)
        body = response.json()
        self.assertEqual((body["save_status"], body["save_reason"]), ("saved", "redacted"))
        saves = [payload for fn, payload in calls if fn == "save_patient_chat_turn"]
        self.assertEqual(len(saves), 1)
        self.assertEqual(saves[0]["p_user_text"], PH_USER)
        self.assertEqual(saves[0]["p_reply_text"], PH_REPLY)
        self.assertEqual(saves[0]["p_reply_mode"], "event")
        self.assertNotIn(self.SECRET, repr(calls))

    async def test_an_ordinary_saved_turn_has_no_redaction_reason(self):
        rec = Recognition('{"intent": "explain_process", "confidence": 0.9}', True)
        with patch("app.patient_chat.generate_reply", AsyncMock(return_value=ChatReply("Your pages are listed in the menu.", "answer"))):
            response, calls = await self.chat("What pages does the portal have?", rec)
        body = response.json()
        self.assertEqual((body["save_status"], body["save_reason"]), ("saved", None))
        saves = [payload for fn, payload in calls if fn == "save_patient_chat_turn"]
        self.assertEqual(saves[0]["p_user_text"], "What pages does the portal have?")


if __name__ == "__main__":
    unittest.main()
