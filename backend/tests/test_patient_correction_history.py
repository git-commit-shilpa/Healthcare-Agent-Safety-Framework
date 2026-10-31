import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.main import CurrentUser, Role, app, get_current_user

HISTORY_PATH = "/api/patient/correction-requests"
SENSITIVE_PROPOSAL = "Fictional proposed treatment (TEST ONLY)"


def _request_row(request_id, *, requested_by=42, decision="Escalate", created_at=None):
    return {
        "request_id": request_id,
        "requested_by": requested_by,
        "guardrail_decision": decision,
        "created_at": created_at or f"2026-10-30T19:4{request_id % 10}:00+00:00",
    }


class SupabaseRouter:
    """Routes mocked _request_supabase calls by table and records each call."""

    def __init__(self, *, requests=None, approvals=None, rpc=None, failures=None):
        self.responses = {
            "/rest/v1/ai_requests": requests if requests is not None else [],
            "/rest/v1/approvals": approvals if approvals is not None else [],
            "/rest/v1/rpc/submit_patient_treatment_correction": rpc,
        }
        self.failures = failures or {}
        self.calls = []

    async def __call__(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if path in self.failures:
            failure = self.failures[path]
            if isinstance(failure, Exception):
                raise failure
            return failure
        response = self.responses[path]
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def calls_to(self, path):
        return [kwargs for called_path, kwargs in self.calls if called_path == path]


class PatientCorrectionHistoryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._key = patch("app.main._service_role_key", return_value="server-only-key")
        self._key.start()

    async def asyncTearDown(self):
        self._key.stop()
        app.dependency_overrides.pop(get_current_user, None)

    def _set_current_user(self, role=Role.PATIENT, user_id=42, permissions=None):
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=user_id,
            email="user@example.test",
            role=role,
            permissions=permissions if permissions is not None else [],
        )

    async def _get(self, query=""):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.get(HISTORY_PATH + query)

    async def _post(self, body=None):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await client.post(
                HISTORY_PATH,
                json=body or {"proposed_treatment": SENSITIVE_PROPOSAL},
            )


class CorrectionHistoryAuthorizationTests(PatientCorrectionHistoryTestCase):
    async def test_unauthenticated_request_is_rejected(self):
        with patch("app.main._request_supabase", new=AsyncMock()) as request:
            response = await self._get()

        self.assertEqual(response.status_code, 401, response.text)
        request.assert_not_awaited()

    async def test_non_patient_roles_are_rejected_without_database_access(self):
        with patch("app.main._request_supabase", new=AsyncMock()) as request:
            for role in (Role.ADMIN, Role.PHYSICIAN, Role.TECHNICIAN):
                self._set_current_user(role, permissions=["view_own_correction_requests"])
                with self.subTest(role=role.value):
                    response = await self._get()
                    self.assertEqual(response.status_code, 403, response.text)

        request.assert_not_awaited()

    async def test_view_permission_is_required(self):
        self._set_current_user(permissions=["submit_record_correction"])
        with patch("app.main._request_supabase", new=AsyncMock()) as request:
            response = await self._get()

        self.assertEqual(response.status_code, 403, response.text)
        request.assert_not_awaited()


class CorrectionHistoryPrivacyTests(PatientCorrectionHistoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._set_current_user(permissions=["view_own_correction_requests"])

    async def test_query_uses_authenticated_user_and_ignores_client_identifiers(self):
        router = SupabaseRouter(
            requests=[_request_row(5)],
            approvals=[{"request_id": 5, "status": "Pending", "reviewed_at": None}],
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._get("?user_id=3&patient_id=102&requested_by=3")

        self.assertEqual(response.status_code, 200, response.text)
        request_params = router.calls_to("/rest/v1/ai_requests")[0]["params"]
        self.assertEqual(request_params["requested_by"], "eq.42")
        self.assertEqual(request_params["action"], "eq.patient_treatment_correction")
        self.assertNotIn("patient_id", request_params)
        approval_params = router.calls_to("/rest/v1/approvals")[0]["params"]
        self.assertEqual(approval_params["request_id"], "in.(5)")

    async def test_sensitive_columns_are_never_selected_or_returned(self):
        router = SupabaseRouter(
            requests=[
                {
                    **_request_row(5),
                    # Extra columns must not be passed through even if returned.
                    "request_details": f'{{"proposed_value":"{SENSITIVE_PROPOSAL}"}}',
                    "decision_reason": "Human review required.",
                    "patient_id": 101,
                }
            ],
            approvals=[
                {"request_id": 5, "status": "Pending", "reviewed_at": None, "reviewer_id": 4}
            ],
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._get()

        self.assertEqual(response.status_code, 200, response.text)
        request_select = router.calls_to("/rest/v1/ai_requests")[0]["params"]["select"]
        approval_select = router.calls_to("/rest/v1/approvals")[0]["params"]["select"]
        for column in ("request_details", "decision_reason", "patient_id", "action"):
            self.assertNotIn(column, request_select)
        self.assertNotIn("reviewer_id", approval_select)

        self.assertNotIn(SENSITIVE_PROPOSAL, response.text)
        item = response.json()["items"][0]
        self.assertEqual(
            set(item),
            {
                "request_id",
                "created_at",
                "guardrail_decision",
                "approval_status",
                "reviewed_at",
                "status_issue",
            },
        )

    async def test_rows_for_another_requester_fail_closed(self):
        router = SupabaseRouter(requests=[_request_row(9, requested_by=3)])
        with patch("app.main._request_supabase", new=router):
            response = await self._get()

        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn("9", response.json()["detail"])
        self.assertEqual(router.calls_to("/rest/v1/approvals"), [])

    async def test_unrelated_approval_rows_fail_closed(self):
        router = SupabaseRouter(
            requests=[_request_row(5)],
            approvals=[{"request_id": 6, "status": "Approved", "reviewed_at": "2026-10-31T00:00:00+00:00"}],
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._get()

        self.assertEqual(response.status_code, 502, response.text)

    async def test_empty_history_skips_approval_lookup(self):
        router = SupabaseRouter(requests=[])
        with patch("app.main._request_supabase", new=router):
            response = await self._get()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"items": []})
        self.assertEqual(router.calls_to("/rest/v1/approvals"), [])

    async def test_supabase_failures_are_reported_without_details(self):
        failures = (
            httpx.Response(500, text="server-only-key details"),
            HTTPException(status_code=503, detail="server-only-key details"),
            httpx.Response(200, text="not json"),
            httpx.Response(200, json={"unexpected": "object"}),
        )
        for failure in failures:
            with self.subTest(failure=repr(failure)[:40]):
                router = SupabaseRouter(failures={"/rest/v1/ai_requests": failure})
                with patch("app.main._request_supabase", new=router):
                    response = await self._get()

                self.assertIn(response.status_code, (502, 503), response.text)
                self.assertEqual(
                    response.json()["detail"],
                    "Your correction request history is temporarily unavailable.",
                )


class CorrectionHistoryStatusMappingTests(PatientCorrectionHistoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._set_current_user(permissions=["view_own_correction_requests"])

    async def _items(self, requests, approvals):
        router = SupabaseRouter(requests=requests, approvals=approvals)
        with patch("app.main._request_supabase", new=router):
            response = await self._get()
        self.assertEqual(response.status_code, 200, response.text)
        return {item["request_id"]: item for item in response.json()["items"]}

    async def test_consistent_statuses_are_returned(self):
        reviewed = "2026-10-31T10:00:00+00:00"
        items = await self._items(
            [_request_row(7), _request_row(6), _request_row(5)],
            [
                {"request_id": 5, "status": "Pending", "reviewed_at": None},
                {"request_id": 6, "status": "Approved", "reviewed_at": reviewed},
                {"request_id": 7, "status": "Rejected", "reviewed_at": reviewed},
            ],
        )

        self.assertEqual(list(items), [7, 6, 5])
        self.assertEqual(
            items[5],
            {
                "request_id": 5,
                "created_at": "2026-10-30T19:45:00+00:00",
                "guardrail_decision": "Escalate",
                "approval_status": "Pending",
                "reviewed_at": None,
                "status_issue": None,
            },
        )
        self.assertEqual(items[6]["approval_status"], "Approved")
        self.assertEqual(items[6]["reviewed_at"], reviewed)
        self.assertEqual(items[7]["approval_status"], "Rejected")
        self.assertIsNone(items[7]["status_issue"])

    async def test_missing_approval_is_an_explicit_inconsistency_not_pending(self):
        with self.assertLogs("app.main", level="WARNING") as logs:
            items = await self._items([_request_row(5)], [])

        self.assertIsNone(items[5]["approval_status"])
        self.assertEqual(items[5]["status_issue"], "missing_approval")
        self.assertTrue(any("missing_approval" in line for line in logs.output))

    async def test_other_inconsistencies_are_flagged(self):
        reviewed = "2026-10-31T10:00:00+00:00"
        with self.assertLogs("app.main", level="WARNING"):
            items = await self._items(
                [
                    _request_row(1),
                    _request_row(2),
                    _request_row(3),
                    _request_row(4),
                    _request_row(5, decision="Maybe"),
                ],
                [
                    {"request_id": 1, "status": "Pending", "reviewed_at": None},
                    {"request_id": 1, "status": "Approved", "reviewed_at": reviewed},
                    {"request_id": 2, "status": "Unknown", "reviewed_at": None},
                    {"request_id": 3, "status": "Pending", "reviewed_at": reviewed},
                    {"request_id": 4, "status": "Approved", "reviewed_at": None},
                    {"request_id": 5, "status": "Pending", "reviewed_at": None},
                ],
            )

        self.assertEqual(items[1]["status_issue"], "multiple_approvals")
        self.assertIsNone(items[1]["approval_status"])
        self.assertEqual(items[2]["status_issue"], "invalid_approval_status")
        self.assertIsNone(items[2]["approval_status"])
        self.assertEqual(items[3]["status_issue"], "review_timestamp_mismatch")
        self.assertEqual(items[4]["status_issue"], "review_timestamp_mismatch")
        self.assertEqual(items[5]["status_issue"], "invalid_guardrail_decision")
        self.assertIsNone(items[5]["guardrail_decision"])

    async def test_decision_casing_is_normalized(self):
        items = await self._items(
            [_request_row(5, decision="escalate")],
            [{"request_id": 5, "status": "Pending", "reviewed_at": None}],
        )
        self.assertEqual(items[5]["guardrail_decision"], "Escalate")


class CorrectionSubmissionDuplicateTests(PatientCorrectionHistoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._set_current_user(permissions=["submit_record_correction"])

    def _rpc_success(self):
        return httpx.Response(
            200,
            json={"request_id": 8, "guardrail_decision": "Escalate", "approval_status": "Pending"},
        )

    async def test_pending_request_blocks_submission_without_rpc(self):
        router = SupabaseRouter(
            requests=[_request_row(5)],
            approvals=[{"request_id": 5, "status": "Pending", "reviewed_at": None}],
            rpc=self._rpc_success(),
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._post()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("#5", response.json()["detail"])
        self.assertIn("awaiting Physician review", response.json()["detail"])
        self.assertEqual(
            router.calls_to("/rest/v1/rpc/submit_patient_treatment_correction"), []
        )
        self.assertEqual(
            router.calls_to("/rest/v1/ai_requests")[0]["params"]["requested_by"], "eq.42"
        )

    async def test_inconsistent_or_unknown_approvals_also_block(self):
        reviewed = "2026-10-31T10:00:00+00:00"
        cases = {
            "missing": [],
            "multiple": [
                {"request_id": 5, "status": "Rejected", "reviewed_at": reviewed},
                {"request_id": 5, "status": "Pending", "reviewed_at": None},
            ],
            "unknown": [{"request_id": 5, "status": "Escalated", "reviewed_at": None}],
        }
        for label, approvals in cases.items():
            with self.subTest(case=label):
                router = SupabaseRouter(
                    requests=[_request_row(5)],
                    approvals=approvals,
                    rpc=self._rpc_success(),
                )
                with patch("app.main._request_supabase", new=router):
                    response = await self._post()

                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    router.calls_to("/rest/v1/rpc/submit_patient_treatment_correction"), []
                )

    async def test_reviewed_requests_allow_a_new_submission(self):
        reviewed = "2026-10-31T10:00:00+00:00"
        router = SupabaseRouter(
            requests=[_request_row(6), _request_row(5)],
            approvals=[
                {"request_id": 5, "status": "Approved", "reviewed_at": reviewed},
                {"request_id": 6, "status": "Rejected", "reviewed_at": reviewed},
            ],
            rpc=self._rpc_success(),
        )
        with patch("app.main._request_supabase", new=router):
            response = await self._post()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["request_id"], 8)
        self.assertEqual(
            len(router.calls_to("/rest/v1/rpc/submit_patient_treatment_correction")), 1
        )

    async def test_first_submission_with_no_history_reaches_rpc(self):
        router = SupabaseRouter(requests=[], rpc=self._rpc_success())
        with patch("app.main._request_supabase", new=router):
            response = await self._post()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(router.calls_to("/rest/v1/approvals"), [])

    async def test_precheck_failure_fails_closed_without_rpc(self):
        for failure in (
            httpx.Response(500, text="details"),
            HTTPException(status_code=503, detail="details"),
        ):
            with self.subTest(failure=type(failure).__name__):
                router = SupabaseRouter(
                    failures={"/rest/v1/ai_requests": failure},
                    rpc=self._rpc_success(),
                )
                with patch("app.main._request_supabase", new=router):
                    response = await self._post()

                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(
                    response.json()["detail"],
                    "Correction requests are temporarily unavailable.",
                )
                self.assertEqual(
                    router.calls_to("/rest/v1/rpc/submit_patient_treatment_correction"),
                    [],
                )

    async def test_precheck_runs_only_after_permission_and_input_validation(self):
        router = SupabaseRouter(rpc=self._rpc_success())
        with patch("app.main._request_supabase", new=router):
            self._set_current_user(permissions=[])
            forbidden = await self._post()
            self._set_current_user(permissions=["submit_record_correction"])
            invalid = await self._post({"proposed_treatment": "   "})

        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(router.calls, [])

    async def test_database_enforced_duplicate_error_maps_to_conflict(self):
        router = SupabaseRouter(
            requests=[],
            rpc=httpx.Response(
                409,
                json={
                    "code": "PT409",
                    "message": "A treatment correction request is already awaiting review.",
                    "details": SENSITIVE_PROPOSAL,
                },
            ),
        )
        with (
            patch("app.main._request_supabase", new=router),
            self.assertLogs("app.main", level="ERROR") as logs,
        ):
            response = await self._post()

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("already awaiting Physician review", response.json()["detail"])
        self.assertNotIn(SENSITIVE_PROPOSAL, response.text + "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
