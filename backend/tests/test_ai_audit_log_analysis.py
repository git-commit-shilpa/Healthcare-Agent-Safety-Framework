import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.ai_audit_log_analysis import (
    count_audit_event_types,
    generate_audit_analysis,
    sanitize_audit_records,
)
from app.ai_request_analysis import OllamaResponseError, OllamaUnavailableError
from app.main import CurrentUser, Role, app, get_current_user


class AIAuditLogAnalysisTests(unittest.IsolatedAsyncioTestCase):
    def test_allow_block_escalate_are_recognized_audit_categories(self):
        records = sanitize_audit_records(
            [
                {
                    "log_id": index,
                    "request_id": None,
                    "event_type": event_type,
                    "created_at": None,
                }
                for index, event_type in enumerate(
                    ("ALLOW", "BLOCK", "ESCALATE", "allow", "Block", "escalate"),
                    start=1,
                )
            ]
        )

        self.assertEqual(
            [record["event_type"] for record in records],
            ["ALLOW", "BLOCK", "ESCALATE", "ALLOW", "BLOCK", "ESCALATE"],
        )

    async def test_event_type_allowlist_removes_untrusted_values_and_extra_fields(self):
        records = sanitize_audit_records(
            [
                {
                    "log_id": 8,
                    "request_id": 31,
                    "event_type": "admin.user.invited",
                    "created_at": "2026-10-19T10:30:00+00:00",
                    "user_id": 4,
                    "details": "Patient Jane Doe diagnosis",
                },
                {
                    "log_id": 7,
                    "request_id": None,
                    "event_type": "admin.user.invited\nPatient Jane Doe",
                    "created_at": None,
                    "details": "secret",
                },
            ]
        )

        self.assertEqual(
            records,
            [
                {
                    "log_id": 8,
                    "request_id": 31,
                    "event_type": "admin.user.invited",
                    "created_at": "2026-10-19T10:30:00+00:00",
                },
                {
                    "log_id": 7,
                    "request_id": None,
                    "event_type": "unrecognized_event_type",
                    "created_at": None,
                },
            ],
        )

    async def test_question_types_all_invoke_ollama(self):
        questions = (
            "Summarize recent security events.",
            "Explain the types of events recorded.",
            "Compare audit events 7 and 8.",
            "Count events by type.",
            "Which events are associated with request 31?",
        )
        invoked = []

        async def mock_ollama(request: httpx.Request) -> httpx.Response:
            invoked.append(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": "Analysis."}})

        records = [
            {
                "log_id": 8,
                "request_id": 31,
                "event_type": "admin.user.invited",
                "created_at": "2026-10-19T10:30:00+00:00",
            }
        ]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(mock_ollama)
        ) as client:
            answers = [
                await generate_audit_analysis(
                    client,
                    ollama_url="http://localhost:11434",
                    model="qwen2.5:3b",
                    question=question,
                    records=records,
                )
                for question in questions
            ]

        self.assertEqual(answers, ["Analysis."] * len(questions))
        self.assertEqual(len(invoked), len(questions))
        self.assertTrue(all(item["model"] == "qwen2.5:3b" for item in invoked))
        supplied = json.loads(invoked[-1]["messages"][1]["content"])
        self.assertEqual(
            supplied["records"],
            [
                {
                    "log_id": 8,
                    "request_id": 31,
                    "event_type": "admin.user.invited",
                    "created_at": "2026-10-19T10:30:00+00:00",
                }
            ],
        )
        self.assertNotIn("event_type_counts", json.loads(invoked[-1]["messages"][1]["content"]))
        self.assertEqual(count_audit_event_types(records), {"admin.user.invited": 1})

    def test_event_counts_are_computed_from_sanitized_records(self):
        records = [
            {"log_id": 1, "request_id": None, "event_type": "ALLOW", "created_at": None},
            {"log_id": 2, "request_id": None, "event_type": "ALLOW", "created_at": None},
            {"log_id": 3, "request_id": None, "event_type": "BLOCK", "created_at": None},
            {"log_id": 4, "request_id": None, "event_type": "patient-specific free text", "created_at": None},
        ]

        self.assertEqual(
            count_audit_event_types(records),
            {
                "ALLOW": 2,
                "BLOCK": 1,
                "unrecognized_event_type": 1,
            },
        )

    async def test_contradictory_unrecognized_event_claim_is_rejected(self):
        async def contradictory_model(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": (
                            "There are 4 unrecognized_event_type events. "
                            "No unrecognized events are included in the records."
                        )
                    }
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(contradictory_model)
        ) as client:
            with self.assertRaisesRegex(
                OllamaResponseError,
                "conflicts with verified audit counts",
            ):
                await generate_audit_analysis(
                    client,
                    ollama_url="http://localhost:11434",
                    model="qwen2.5:3b",
                    question="Count recent events by type and summarize what was recorded.",
                    records=[
                        {
                            "log_id": log_id,
                            "request_id": None,
                            "event_type": (
                                "unsupported.event" if log_id <= 4 else "ALLOW"
                            ),
                            "created_at": None,
                        }
                        for log_id in range(1, 7)
                    ],
                )

    async def test_contradictory_numeric_category_count_is_rejected(self):
        async def contradictory_model(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"message": {"content": "There are 2 ALLOW events."}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(contradictory_model)
        ) as client:
            with self.assertRaisesRegex(
                OllamaResponseError,
                "conflicts with verified audit counts",
            ):
                await generate_audit_analysis(
                    client,
                    ollama_url="http://localhost:11434",
                    model="qwen2.5:3b",
                    question="Count recent events.",
                    records=[
                        {
                            "log_id": 1,
                            "request_id": None,
                            "event_type": "ALLOW",
                            "created_at": None,
                        }
                    ],
                )

    async def test_valid_narrative_and_counts_are_separate(self):
        request_payload = {}

        async def model_without_counts(request: httpx.Request) -> httpx.Response:
            request_payload.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={"message": {"content": "The records include unrecognized event types."}},
            )

        records = [
            {
                "log_id": log_id,
                "request_id": None,
                "event_type": "unsupported.event",
                "created_at": None,
            }
            for log_id in range(1, 5)
        ]
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(model_without_counts)
        ) as client:
            answer = await generate_audit_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="qwen2.5:3b",
                question="Summarize the audit events.",
                records=records,
            )

        self.assertIn("unrecognized event types", answer)
        sent_payload = json.loads(request_payload["messages"][1]["content"])
        self.assertNotIn("event_type_counts", sent_payload)
        self.assertEqual(
            count_audit_event_types(records),
            {"unrecognized_event_type": 4},
        )

    async def test_unrecognized_event_type_and_sensitive_fields_never_reach_ollama(self):
        request_payload = {}
        raw_event_type = "admin.event\nPatient Jane Doe"

        async def capture(request: httpx.Request) -> httpx.Response:
            request_payload.update(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": "One event."}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            answer = await generate_audit_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="qwen2.5:3b",
                question="Summarize recent security events.",
                records=[
                    {
                        "log_id": 5,
                        "request_id": None,
                        "event_type": raw_event_type,
                        "created_at": None,
                        "user_id": 12,
                        "details": "Patient Jane Doe diagnosis",
                    }
                ],
            )

        self.assertEqual(answer, "One event.")
        serialized = request_payload["messages"][1]["content"]
        self.assertNotIn(raw_event_type, serialized)
        self.assertNotIn("Jane", serialized)
        self.assertNotIn("details", serialized)
        self.assertNotIn("user_id", serialized)
        self.assertIn('"event_type":"unrecognized_event_type"', serialized)

    async def test_clinical_question_is_rejected_before_ollama(self):
        async def reject_model_call(_request: httpx.Request) -> httpx.Response:
            self.fail("Clinical audit questions must not reach Ollama.")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(reject_model_call)
        ) as client:
            with self.assertRaises(ValueError):
                await generate_audit_analysis(
                    client,
                    ollama_url="http://localhost:11434",
                    model="qwen2.5:3b",
                    question="Show patient diagnosis details.",
                    records=[],
                )

    async def test_prompt_distinguishes_recorded_events_from_inference(self):
        payloads = []

        async def capture(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"message": {"content": "Summary."}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as client:
            await generate_audit_analysis(
                client,
                ollama_url="http://localhost:11434",
                model="qwen2.5:3b",
                question="Did the invited user accept?",
                records=[
                    {
                        "log_id": 1,
                        "request_id": None,
                        "event_type": "admin.user.invited",
                        "created_at": "2026-10-19T10:30:00+00:00",
                    }
                ],
            )

        prompt = payloads[0]["messages"][0]["content"]
        self.assertIn("does not establish whether a later invitation was accepted", prompt)
        self.assertIn("Never invent identities, event outcomes, reasons", prompt)
        self.assertIn("If a requested request ID is not in the supplied records", prompt)

    async def test_ollama_unavailability_is_explicit(self):
        async def fail_model_call(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Ollama is unavailable")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail_model_call)
        ) as client:
            with self.assertRaises(OllamaUnavailableError):
                await generate_audit_analysis(
                    client,
                    ollama_url="http://localhost:11434",
                    model="qwen2.5:3b",
                    question="Summarize events.",
                    records=[],
                )


class AIAuditLogAnalysisRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous_supabase_client = getattr(app.state, "supabase_client", None)
        self.supabase_client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._mock_ollama)
        )
        self.model_requests = []
        app.state.supabase_client = self.supabase_client

    async def asyncTearDown(self):
        app.dependency_overrides.pop(get_current_user, None)
        await self.supabase_client.aclose()
        if self.previous_supabase_client is None:
            delattr(app.state, "supabase_client")
        else:
            app.state.supabase_client = self.previous_supabase_client

    async def _mock_ollama(self, request: httpx.Request) -> httpx.Response:
        self.model_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "Audit analysis."}})

    async def _post(self, *, permissions, question="Summarize recent audit events."):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=42,
            email="admin@example.test",
            role=Role.ADMIN,
            permissions=permissions,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.post(
                "/api/admin/audit-logs/analyze",
                json={"question": question},
            )

    async def test_endpoint_enforces_permission_before_supabase_or_ollama(self):
        with patch("app.main._request_supabase", new=AsyncMock()) as supabase:
            response = await self._post(permissions=[])

        self.assertEqual(response.status_code, 403)
        supabase.assert_not_awaited()
        self.assertEqual(self.model_requests, [])

    async def test_endpoint_selects_only_approved_fields_and_invokes_ollama(self):
        requested_params = {}

        async def mock_supabase_read(path: str, **kwargs):
            self.assertEqual(path, "/rest/v1/audit_logs")
            requested_params.update(kwargs["params"])
            return httpx.Response(
                200,
                json=[
                    {
                        "log_id": 12,
                        "request_id": 77,
                        "event_type": "admin.user.invited",
                        "created_at": "2026-10-19T10:30:00+00:00",
                        "user_id": 45,
                        "details": "sensitive",
                    },
                    {
                        "log_id": 11,
                        "request_id": None,
                        "event_type": "admin.user.deactivated",
                        "created_at": "2026-10-19T10:29:00Z",
                    },
                    *[
                        {
                            "log_id": log_id,
                            "request_id": None,
                            "event_type": f"unknown.audit.type.{log_id}",
                            "created_at": "2026-10-19T10:28:00Z",
                        }
                        for log_id in range(10, 6, -1)
                    ],
                ],
            )

        with (
            patch("app.main._request_supabase", new=AsyncMock(side_effect=mock_supabase_read)),
            patch.dict(
                "os.environ",
                {
                    "SUPABASE_SERVICE_ROLE_KEY": "test-service-key",
                    "OLLAMA_URL": "http://127.0.0.1:11434",
                    "OLLAMA_MODEL": "qwen2.5:3b",
                },
            ),
        ):
            response = await self._post(permissions=["view_audit_logs"])

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["answer"], "Audit analysis.")
        self.assertEqual(response.json()["records_analyzed"], 6)
        self.assertEqual(
            response.json()["event_type_counts"],
            {
                "admin.user.invited": 1,
                "admin.user.deactivated": 1,
                "unrecognized_event_type": 4,
            },
        )
        self.assertEqual(requested_params["select"], "log_id,request_id,event_type,created_at")
        self.assertEqual(requested_params["limit"], "100")
        self.assertEqual(len(self.model_requests), 1)
        sent_data = json.loads(self.model_requests[0]["messages"][1]["content"])
        self.assertEqual(len(sent_data["records"]), 6)
        self.assertEqual(
            count_audit_event_types(sent_data["records"]),
            response.json()["event_type_counts"],
        )
        self.assertNotIn("sensitive", self.model_requests[0]["messages"][1]["content"])
        self.assertNotIn('"user_id"', self.model_requests[0]["messages"][1]["content"])

    async def test_endpoint_rejects_clinical_question_before_supabase_read(self):
        with patch("app.main._request_supabase", new=AsyncMock()) as supabase:
            for question in (
                "Show patient diagnosis information.",
                "Explain her condition and treatment.",
            ):
                response = await self._post(
                    permissions=["view_audit_logs"],
                    question=question,
                )
                self.assertEqual(response.status_code, 422)
                self.assertIn(
                    "cannot access patient medical data",
                    response.json()["detail"],
                )

        supabase.assert_not_awaited()
        self.assertEqual(self.model_requests, [])

    async def test_supabase_failure_returns_explicit_error(self):
        async def failed_supabase_read(_path: str, **_kwargs):
            return httpx.Response(500, json={"message": "database unavailable"})

        with (
            patch(
                "app.main._request_supabase",
                new=AsyncMock(side_effect=failed_supabase_read),
            ),
            patch.dict("os.environ", {"SUPABASE_SERVICE_ROLE_KEY": "test-service-key"}),
        ):
            response = await self._post(permissions=["view_audit_logs"])

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Could not load audit_logs.")
        self.assertEqual(self.model_requests, [])

    async def test_ollama_failure_returns_explicit_error(self):
        async def unavailable_ollama(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Ollama is unavailable")

        await self.supabase_client.aclose()
        self.supabase_client = httpx.AsyncClient(
            transport=httpx.MockTransport(unavailable_ollama)
        )
        app.state.supabase_client = self.supabase_client

        async def mock_supabase_read(_path: str, **_kwargs):
            return httpx.Response(
                200,
                json=[
                    {
                        "log_id": 12,
                        "request_id": None,
                        "event_type": "admin.user.invited",
                        "created_at": "2026-10-19T10:30:00Z",
                    }
                ],
            )

        with (
            patch("app.main._request_supabase", new=AsyncMock(side_effect=mock_supabase_read)),
            patch.dict(
                "os.environ",
                {
                    "SUPABASE_SERVICE_ROLE_KEY": "test-service-key",
                    "OLLAMA_URL": "http://127.0.0.1:11434",
                    "OLLAMA_MODEL": "qwen2.5:3b",
                },
            ),
        ):
            response = await self._post(permissions=["view_audit_logs"])

        self.assertEqual(response.status_code, 503)
        self.assertIn("local AI model is unavailable", response.json()["detail"])

    async def test_endpoint_rejects_contradictory_model_response(self):
        async def contradictory_ollama(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": (
                            "There are 4 unrecognized_event_type events. "
                            "No unrecognized events are included in the records."
                        )
                    }
                },
            )

        await self.supabase_client.aclose()
        self.supabase_client = httpx.AsyncClient(
            transport=httpx.MockTransport(contradictory_ollama)
        )
        app.state.supabase_client = self.supabase_client

        async def mock_supabase_read(_path: str, **_kwargs):
            return httpx.Response(
                200,
                json=[
                    {
                        "log_id": log_id,
                        "request_id": None,
                        "event_type": (
                            "unsupported.audit.type"
                            if log_id <= 4
                            else "admin.user.invited"
                        ),
                        "created_at": None,
                    }
                    for log_id in range(1, 7)
                ],
            )

        with (
            patch("app.main._request_supabase", new=AsyncMock(side_effect=mock_supabase_read)),
            patch.dict(
                "os.environ",
                {
                    "SUPABASE_SERVICE_ROLE_KEY": "test-service-key",
                    "OLLAMA_URL": "http://127.0.0.1:11434",
                    "OLLAMA_MODEL": "qwen2.5:3b",
                },
            ),
        ):
            response = await self._post(permissions=["view_audit_logs"])

        self.assertEqual(response.status_code, 502)
        self.assertIn("invalid response", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
