import json
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

import httpx

from app.ai_request_analysis import (
    OllamaUnavailableError,
    _build_messages,
    generate_analysis,
    sanitize_analysis_records,
)
from app.main import CurrentUser, Role, app, get_current_user


class AIRequestAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def analyze_with_mocked_ollama(self, question, records, answer="Model answer."):
        requests = []

        async def capture_model_request(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": answer}})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(capture_model_request)
        ) as client:
            result = await generate_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="test-model",
                question=question,
                records=records,
            )
        return result, requests

    async def test_development_log_marks_deterministic_answer_without_question_data(self):
        question = "Why was request 3 escalated for Jane Doe?"

        async def reject_model_call(_request: httpx.Request) -> httpx.Response:
            self.fail("A deterministic guardrail question must not invoke Ollama.")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(reject_model_call)
        ) as client:
            with patch.dict("os.environ", {"APP_ENV": "development"}):
                with self.assertLogs("app.ai_request_analysis", level="INFO") as logs:
                    await generate_analysis(
                        client,
                        ollama_url="http://localhost:11434",
                        model="test-model",
                        question=question,
                        records=[],
                    )

        self.assertEqual(len(logs.output), 1)
        self.assertIn("ollama_invoked=false", logs.output[0])
        self.assertIn("answer_path=deterministic_guardrail", logs.output[0])
        self.assertNotIn("Jane", logs.output[0])
        self.assertNotIn(question, logs.output[0])

    async def test_development_log_marks_ollama_invocation_without_question_data(self):
        question = "Summarize blocked requests. ref-QX-739."
        captured = []

        async def capture_model_request(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"message": {"content": "Summary."}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(capture_model_request)
        ) as client:
            with patch.dict("os.environ", {"APP_ENV": "development"}):
                with self.assertLogs("app.ai_request_analysis", level="INFO") as logs:
                    await generate_analysis(
                        client,
                        ollama_url="http://localhost:11434",
                        model="test-model",
                        question=question,
                        records=[],
                    )

        self.assertEqual(len(captured), 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ollama_invoked=true", logs.output[0])
        self.assertIn("answer_path=ollama_api_chat", logs.output[0])
        self.assertNotIn("QX-739", logs.output[0])
        self.assertNotIn(question, logs.output[0])

    async def test_development_logging_is_disabled_outside_development(self):
        async def reject_model_call(_request: httpx.Request) -> httpx.Response:
            self.fail("A deterministic guardrail question must not invoke Ollama.")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(reject_model_call)
        ) as client:
            with patch.dict("os.environ", {"APP_ENV": "production"}):
                with self.assertNoLogs("app.ai_request_analysis", level="INFO"):
                    await generate_analysis(
                        client,
                        ollama_url="http://localhost:11434",
                        model="test-model",
                        question="Show me the patient's diagnosis.",
                        records=[],
                    )

    async def test_named_patient_clinical_question_is_rejected_before_model_call(self):
        async def reject_model_call(_request: httpx.Request) -> httpx.Response:
            self.fail(f"Patient medical questions must not be sent to the model: {_request.url}")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(reject_model_call)
        ) as client:
            answer = await generate_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="test-model",
                question="Show me Emily Johnson's medical condition and treatment.",
                records=[
                    {
                        "request_id": 2,
                        "action": "prescribe",
                        "guardrail_decision": "Block",
                        "created_at": "2026-10-19T10:00:00Z",
                    },
                    {
                        "request_id": 4,
                        "action": "diagnose",
                        "guardrail_decision": "Escalate",
                        "created_at": "2026-10-19T09:00:00Z",
                    },
                ],
            )

        self.assertIn("Admin AI cannot access patient medical data", answer)
        self.assertNotIn("Emily", answer)
        self.assertNotIn("request 2", answer)
        self.assertNotIn("request 4", answer)

    async def test_patient_medical_information_is_rejected_before_model_call(self):
        async def reject_model_call(_request: httpx.Request) -> httpx.Response:
            self.fail(f"Patient medical questions must not be sent to the model: {_request.url}")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(reject_model_call)
        ) as client:
            answer = await generate_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="test-model",
                question="What medical condition is request 2 for?",
                records=[],
            )

        self.assertIn("Admin AI cannot access patient medical data", answer)

    async def test_multi_request_analysis_uses_model_for_all_records(self):
        captured = []

        async def answer_metadata_question(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/chat")
            payload = json.loads(request.content)
            captured.append(payload)
            return httpx.Response(
                200,
                json={"message": {"content": "Request 17 was blocked; request 18 was escalated."}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(answer_metadata_question)
        ) as client:
            answer = await generate_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="test-model",
                question="Compare the outcomes and reasons for requests 17 and 18.",
                records=[
                    {
                        "request_id": 17,
                        "action": "prescribe",
                        "guardrail_decision": "Block",
                        "created_at": "2026-10-19T10:00:00Z",
                    },
                    {
                        "request_id": 18,
                        "action": "modify_medical_record",
                        "guardrail_decision": "Escalate",
                        "created_at": "2026-10-19T09:00:00Z",
                        "decision_reason": "Human approval required",
                    },
                ],
            )

        self.assertEqual(answer, "Request 17 was blocked; request 18 was escalated.")
        self.assertEqual(len(captured), 1)
        sent_records = json.loads(captured[0]["messages"][1]["content"])["records"]
        self.assertEqual([record["request_id"] for record in sent_records], [17, 18])
        self.assertEqual(
            sent_records[1]["approved_reason"],
            "the proposed action requires human approval before proceeding",
        )

    async def test_allowlisted_reason_is_curated_and_raw_reason_never_reaches_model(self):
        raw_reason = "  UNAUTHORIZED   ACCESS to another patient record "
        request_payload = {}

        async def capture_model_request(request: httpx.Request) -> httpx.Response:
            request_payload.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"message": {"content": "A grounded summary."}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(capture_model_request)
        ) as client:
            answer = await generate_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="test-model",
                question="Summarize the request outcomes.",
                records=[
                    {
                        "request_id": 2,
                        "action": "view_other_patient_record",
                        "guardrail_decision": "Block",
                        "created_at": "2026-10-19T10:00:00Z",
                        "decision_reason": raw_reason,
                    }
                ],
            )

        records_sent = json.loads(request_payload["messages"][1]["content"])["records"]
        self.assertEqual(answer, "A grounded summary.")
        self.assertEqual(
            records_sent[0]["approved_reason"],
            "access to another patient's record was not authorized",
        )
        self.assertNotIn("decision_reason", records_sent[0])
        self.assertNotIn(raw_reason, json.dumps(request_payload))

    def test_only_the_four_approved_reason_phrases_are_mapped(self):
        reasons = (
            "Own patient record",
            "Unauthorized access to another patient record",
            "Human approval required",
            "Technician lacks permission",
        )
        safe_records = sanitize_analysis_records(
            [{"request_id": index, "decision_reason": reason} for index, reason in enumerate(reasons)]
        )

        self.assertEqual(
            [record["approved_reason"] for record in safe_records],
            [
                "the request involved access to the requester's own patient record",
                "access to another patient's record was not authorized",
                "the proposed action requires human approval before proceeding",
                "the Technician role does not have permission to modify patient medical records",
            ],
        )

    async def test_unapproved_reason_containing_patient_data_is_dropped(self):
        raw_reason = "Unauthorized access to another patient record: Emily Johnson diagnosis"
        request_payload = {}

        async def capture_model_request(request: httpx.Request) -> httpx.Response:
            request_payload.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"message": {"content": "A metadata summary."}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(capture_model_request)
        ) as client:
            answer = await generate_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="test-model",
                question="Summarize the request outcomes.",
                records=[
                    {
                        "request_id": 2,
                        "action": "view_other_patient_record",
                        "guardrail_decision": "Block",
                        "created_at": "2026-10-19T10:00:00Z",
                        "decision_reason": raw_reason,
                    }
                ],
            )

        payload_text = json.dumps(request_payload)
        self.assertEqual(answer, "A metadata summary.")
        self.assertNotIn("Emily", payload_text)
        self.assertNotIn("diagnosis", payload_text.casefold())
        self.assertNotIn(raw_reason, payload_text)
        sent_records = json.loads(
            request_payload["messages"][1]["content"]
        )["records"]
        self.assertNotIn("approved_reason", sent_records[0])

    async def test_block_rationale_is_distinguished_from_recorded_decision(self):
        answer, requests = await self.analyze_with_mocked_ollama(
            "Why was request 17 blocked?",
            [
                {
                    "request_id": 17,
                    "action": "prescribe",
                    "guardrail_decision": "Block",
                    "created_at": "2026-10-19T10:00:00Z",
                }
            ],
            "Request #17 was blocked. The action was prescribing. The exact "
            "recorded reason is unavailable; prescribing without authorization "
            "could pose a safety risk.",
        )

        self.assertIn("recorded reason is unavailable", answer)
        self.assertEqual(len(requests), 1)
        self.assertIn("exact recorded reason is unavailable", requests[0]["messages"][0]["content"])

    async def test_block_rationale_explains_action_and_conditional_security_risk(self):
        answer, requests = await self.analyze_with_mocked_ollama(
            "Why exactly was request 2 blocked?",
            [
                {
                    "request_id": 2,
                    "action": "view_other_patient_record",
                    "guardrail_decision": "Block",
                    "created_at": "2026-10-19T10:00:00Z",
                }
            ],
            "Request #2 was blocked. The action was an attempt to view another "
            "patient's medical record. This may create a privacy risk if the "
            "requester lacks authorization. The exact recorded reason is "
            "unavailable.",
        )

        self.assertNotIn("Emily", answer)
        self.assertNotIn("Jane", answer)
        self.assertNotIn("requester was unauthorized", answer)
        self.assertEqual(len(requests), 1)
        serialized = requests[0]["messages"][1]["content"]
        self.assertIn("view_other_patient_record", serialized)
        self.assertNotIn("Emily", serialized)
        self.assertNotIn("Jane", serialized)

    async def test_allowlisted_guardrail_reason_is_reported_as_recorded_fact(self):
        answer, requests = await self.analyze_with_mocked_ollama(
            "Why was request 2 blocked?",
            [
                {
                    "request_id": 2,
                    "action": "view_other_patient_record",
                    "guardrail_decision": "Block",
                    "created_at": "2026-10-19T10:00:00Z",
                    "decision_reason": "Unauthorized access to another patient record",
                }
            ],
            "Request #2 was blocked because access to another patient's record "
            "was not authorized. This is the approved recorded guardrail reason. "
            "This helps protect patient privacy.",
        )

        self.assertIn(
            "Request #2 was blocked because access to another patient's record "
            "was not authorized.",
            answer,
        )
        self.assertIn("helps protect patient privacy", answer)
        self.assertEqual(len(requests), 1)
        safe_records = json.loads(requests[0]["messages"][1]["content"])["records"]
        self.assertEqual(
            safe_records[0]["approved_reason"],
            "access to another patient's record was not authorized",
        )
        self.assertNotIn("decision_reason", safe_records[0])
        self.assertNotIn(
            "Unauthorized access to another patient record",
            requests[0]["messages"][1]["content"],
        )

    async def test_request_four_technician_reason_explains_recorded_permission(self):
        answer, requests = await self.analyze_with_mocked_ollama(
            "Why was request #4 blocked?",
            [
                {
                    "request_id": 4,
                    "action": "modify_medical_record",
                    "guardrail_decision": "Block",
                    "created_at": "2026-10-19T10:00:00Z",
                    "decision_reason": "Technician lacks permission",
                }
            ],
            "Request #4 was blocked because the Technician role does not have "
            "permission to modify patient medical records. This restriction "
            "helps prevent unauthorized modifications to patient information.",
        )

        self.assertEqual(
            answer,
            "Request #4 was blocked because the Technician role does not have "
            "permission to modify patient medical records. This restriction "
            "helps prevent unauthorized modifications to patient information.",
        )
        self.assertNotIn("decision_reason", answer)
        self.assertNotIn("patient name", answer.casefold())
        self.assertEqual(len(requests), 1)

    async def test_request_three_human_approval_reason_is_explained(self):
        expected = (
            "Request #3 was escalated because the proposed action requires human "
            "approval before proceeding. The system referred the request for "
            "authorized review rather than executing it automatically."
        )
        answer, requests = await self.analyze_with_mocked_ollama(
            "Why exactly was request 3 escalated?",
            [
                {
                    "request_id": 3,
                    "action": "modify_medical_record",
                    "guardrail_decision": "Escalate",
                    "created_at": "2026-10-19T10:00:00Z",
                    "decision_reason": "Human approval required",
                }
            ],
            expected,
        )

        self.assertEqual(answer, expected)
        self.assertEqual(len(requests), 1)

    async def test_ollama_unavailability_raises_instead_of_returning_fallback(self):
        async def fail_model_call(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("model unavailable")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail_model_call)
        ) as client:
            with self.assertRaises(OllamaUnavailableError):
                await generate_analysis(
                    client,
                    ollama_url="http://localhost:11434",
                    model="test-model",
                    question="Summarize request decisions.",
                    records=[],
                )

    async def test_allowed_request_reason_is_explained(self):
        answer, requests = await self.analyze_with_mocked_ollama(
            "Why was request 5 allowed?",
            [
                {
                    "request_id": 5,
                    "action": "view_own_patient_record",
                    "guardrail_decision": "Allow",
                    "created_at": "2026-10-19T10:00:00Z",
                    "decision_reason": "Own patient record",
                }
            ],
            "Request #5 was allowed because the request involved access to the "
            "requester's own patient record. This access should still follow the "
            "requester's role and applicable policy.",
        )
        self.assertEqual(len(requests), 1)

    async def test_named_patient_is_not_linked_to_a_request(self):
        for question in (
            "Is Jane Doe associated with request 17?",
            "Is Jane associated with request 17?",
        ):
            with self.subTest(question=question):
                async def reject_model_call(_request: httpx.Request) -> httpx.Response:
                    self.fail(f"Unexpected model call: {_request.url}")

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(reject_model_call)
                ) as client:
                    answer = await generate_analysis(
                        client,
                        ollama_url="http://localhost:11434",
                        model="test-model",
                        question=question,
                        records=[
                            {
                                "request_id": 17,
                                "action": "prescribe",
                                "guardrail_decision": "Block",
                                "created_at": "2026-10-19T10:00:00Z",
                            }
                        ],
                    )

                self.assertIn("no patient identities", answer)
                self.assertIn("cannot establish", answer)
                self.assertNotIn("Jane", answer)
                self.assertNotIn("request 17", answer)

    def test_system_prompt_distinguishes_decisions_and_patient_linkage(self):
        system_message = _build_messages("Summarize the records.", [])[0]["content"]

        self.assertIn("recorded guardrail outcome, not an explanation", system_message)
        self.assertIn("exact recorded reason is unavailable", system_message)
        self.assertIn("Never infer that a named person is a patient", system_message)
        self.assertIn("cannot establish it", system_message)
        self.assertIn("raw `decision_reason` text is never provided", system_message)


class AIRequestAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_escalated_request_analysis_route_uses_mocked_supabase_reason(self):
        request_params = {}
        captured_model_requests = []

        async def mock_supabase_read(path: str, **kwargs):
            self.assertEqual(path, "/rest/v1/ai_requests")
            request_params.update(kwargs.get("params", {}))
            return httpx.Response(
                200,
                json=[
                    {
                        "request_id": 3,
                        "action": "update_assigned_record",
                        "guardrail_decision": "Escalate",
                        "created_at": "2026-10-19T10:00:00Z",
                        "decision_reason": "Human approval required",
                    }
                ],
            )

        async def mock_ollama(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/chat")
            payload = json.loads(request.content)
            captured_model_requests.append(payload)
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": (
                            "Request #3 was escalated because the proposed action "
                            "requires human approval before proceeding. The system "
                            "referred the request for authorized review rather than "
                            "executing it automatically."
                        )
                    }
                },
            )

        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=42,
            email="admin@example.test",
            role=Role.ADMIN,
            permissions=["monitor_ai_requests"],
        )
        previous_supabase_client = getattr(app.state, "supabase_client", None)
        test_supabase_client = httpx.AsyncClient(
            transport=httpx.MockTransport(mock_ollama)
        )
        app.state.supabase_client = test_supabase_client
        try:
            with (
                patch("app.main._request_supabase", new=AsyncMock(side_effect=mock_supabase_read)),
                patch.dict(
                    "os.environ",
                    {
                        "SUPABASE_SERVICE_ROLE_KEY": "test-service-key",
                        "OLLAMA_URL": "http://127.0.0.1:11434",
                        "OLLAMA_MODEL": "test-model",
                    },
                ),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    response = await client.post(
                        "/api/admin/ai-requests/analyze",
                        json={"question": "Why was request 3 escalated?"},
                    )
        finally:
            app.dependency_overrides.pop(get_current_user, None)
            await test_supabase_client.aclose()
            if previous_supabase_client is None:
                delattr(app.state, "supabase_client")
            else:
                app.state.supabase_client = previous_supabase_client

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(captured_model_requests), 1)
        self.assertEqual(
            response.json()["answer"],
            "Request #3 was escalated because the proposed action requires human "
            "approval before proceeding. The system referred the request for "
            "authorized review rather than executing it automatically.",
        )
        self.assertIn("decision_reason", request_params["select"])
        sent_payload = captured_model_requests[0]["messages"][1]["content"]
        self.assertNotIn("Human approval required", sent_payload)
        self.assertIn("proposed action requires human approval", sent_payload)
        self.assertNotIn("decision_reason", sent_payload)

    async def test_user_without_monitor_permission_cannot_call_analysis_endpoint(self):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=42,
            email="user@example.test",
            role=Role.ADMIN,
            permissions=[],
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/admin/ai-requests/analyze",
                    json={"question": "Summarize blocked requests"},
                )
        finally:
            app.dependency_overrides.pop(get_current_user, None)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "You do not have permission to perform this action.",
        )


if __name__ == "__main__":
    unittest.main()
