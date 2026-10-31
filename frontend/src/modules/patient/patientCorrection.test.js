import assert from "node:assert/strict";
import test from "node:test";

import {
  PatientApiError,
  patientRequest,
  submitTreatmentCorrection,
} from "./patientApi.js";
import {
  MAX_PROPOSED_TREATMENT_LENGTH,
  describeApprovalStatus,
  describeSubmissionError,
  findBlockingRequest,
  parseHistoryResponse,
  parseRecordResponse,
  parseSubmissionResponse,
  validateProposedTreatment,
} from "./patientCorrection.js";

const pending = {
  request_id: 5,
  created_at: "2026-10-30T19:40:44.420685+00:00",
  guardrail_decision: "Escalate",
  approval_status: "Pending",
  reviewed_at: null,
  status_issue: null,
};
const approved = {
  ...pending,
  request_id: 4,
  approval_status: "Approved",
  reviewed_at: "2026-10-31T10:00:00+00:00",
};
const rejected = { ...approved, request_id: 3, approval_status: "Rejected" };

function jsonResponse(status, body) {
  return new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

test("proposal validation requires a changed, non-empty value within the limit", () => {
  assert.match(validateProposedTreatment("   ", "Current"), /Enter the treatment/);
  assert.match(validateProposedTreatment(" Current ", "Current"), /different/);
  assert.match(
    validateProposedTreatment("x".repeat(MAX_PROPOSED_TREATMENT_LENGTH + 1), "Current"),
    /2000 characters/,
  );
  assert.equal(validateProposedTreatment("New fictional plan", "Current"), "");
  assert.equal(validateProposedTreatment("New fictional plan", null), "");
});

test("any request not finally Approved or Rejected blocks a new submission", () => {
  assert.equal(findBlockingRequest([]), null);
  assert.equal(findBlockingRequest([approved, rejected]), null);
  assert.equal(findBlockingRequest([approved, pending]), pending);
  const missing = { ...pending, approval_status: null, status_issue: "missing_approval" };
  assert.equal(findBlockingRequest([approved, missing]), missing);
});

test("inconsistent records are shown as needing attention, never as Pending", () => {
  const missing = { ...pending, approval_status: null, status_issue: "missing_approval" };
  assert.equal(describeApprovalStatus(missing).label, "Needs attention");
  assert.equal(describeApprovalStatus(pending).label, "Pending review");
  assert.equal(describeApprovalStatus(approved).tone, "approved");
  assert.equal(describeApprovalStatus(rejected).tone, "rejected");
});

test("history parsing accepts valid items and rejects unexpected values", () => {
  assert.deepEqual(parseHistoryResponse({ items: [pending, approved] }), [pending, approved]);
  assert.deepEqual(parseHistoryResponse({ items: [] }), []);
  assert.throws(() => parseHistoryResponse({}), /invalid/);
  assert.throws(() => parseHistoryResponse({ items: [{ ...pending, approval_status: "Done" }] }));
  assert.throws(() => parseHistoryResponse({ items: [{ ...pending, request_id: "5" }] }));
  assert.throws(() => parseHistoryResponse({ items: [{ ...pending, status_issue: "other" }] }));
});

test("record parsing supports a missing record and rejects malformed data", () => {
  assert.equal(parseRecordResponse({ record: null }), null);
  const record = { age: 45, medical_condition: "Fictional", treatment: "Fictional plan" };
  assert.deepEqual(parseRecordResponse({ record }), record);
  assert.throws(() => parseRecordResponse({}));
  assert.throws(() => parseRecordResponse({ record: { ...record, age: "45" } }));
});

test("submission confirmation must be an Escalate / Pending result", () => {
  assert.ok(parseSubmissionResponse({ request_id: 6, guardrail_decision: "Escalate", approval_status: "Pending" }));
  assert.equal(parseSubmissionResponse({ request_id: 6, guardrail_decision: "Allow", approval_status: "Approved" }), null);
  assert.equal(parseSubmissionResponse(null), null);
});

test("uncertain outcomes are never described as retryable", () => {
  for (const error of [
    new PatientApiError("x", { kind: "http", status: 504 }),
    new PatientApiError("x", { kind: "http", status: 502 }),
    new PatientApiError("x", { kind: "timeout" }),
    new PatientApiError("x", { kind: "network" }),
    new PatientApiError("x", { kind: "invalid", status: 200 }),
  ]) {
    const description = describeSubmissionError(error);
    assert.equal(description.outcome, "uncertain", `${error.kind}/${error.status}`);
    assert.match(description.message, /Do not submit it again yet/);
  }
});

test("definite failures are distinguished from uncertain ones", () => {
  assert.equal(
    describeSubmissionError(new PatientApiError("Already pending", { kind: "http", status: 409 })).outcome,
    "conflict",
  );
  assert.equal(
    describeSubmissionError(new PatientApiError("Bad", { kind: "http", status: 422 })).outcome,
    "invalid",
  );
  const unavailable = describeSubmissionError(
    new PatientApiError("Correction requests are temporarily unavailable.", { kind: "http", status: 503 }),
  );
  assert.equal(unavailable.outcome, "not-sent");
  assert.match(unavailable.message, /Nothing was submitted/);
  assert.equal(
    describeSubmissionError(new PatientApiError("down", { kind: "unreachable", status: 500 })).outcome,
    "not-sent",
  );
});

test("patientRequest sends the bearer token and returns JSON data", async () => {
  let captured;
  const data = await patientRequest("/patient/correction-requests", "token-123", {
    fetchImpl: async (url, init) => {
      captured = { url, init };
      return jsonResponse(200, { items: [] });
    },
  });
  assert.deepEqual(data, { items: [] });
  assert.equal(captured.url, "/api/patient/correction-requests");
  assert.equal(captured.init.headers.Authorization, "Bearer token-123");
  assert.equal(captured.init.method, "GET");
});

test("submission sends only the proposed treatment", async () => {
  let captured;
  await submitTreatmentCorrection("token-123", "Fictional plan", {
    fetchImpl: async (url, init) => {
      captured = init;
      return jsonResponse(200, { request_id: 6, guardrail_decision: "Escalate", approval_status: "Pending" });
    },
  });
  assert.equal(captured.method, "POST");
  assert.deepEqual(JSON.parse(captured.body), { proposed_treatment: "Fictional plan" });
});

test("patientRequest classifies backend and transport failures", async () => {
  await assert.rejects(
    patientRequest("/x", "t", { fetchImpl: async () => new Response("", { status: 500 }) }),
    (error) => error.kind === "unreachable" && /not reachable/.test(error.message),
  );
  await assert.rejects(
    patientRequest("/x", "t", { fetchImpl: async () => jsonResponse(409, { detail: "Pending #5" }) }),
    (error) => error.kind === "http" && error.status === 409 && error.message === "Pending #5",
  );
  await assert.rejects(
    patientRequest("/x", "t", { fetchImpl: async () => { throw new TypeError("Failed to fetch"); } }),
    (error) => error.kind === "network",
  );
  await assert.rejects(
    patientRequest("/x", "t", {
      timeoutMs: 10,
      fetchImpl: (url, init) =>
        new Promise((resolve, reject) => {
          init.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        }),
    }),
    (error) => error.kind === "timeout",
  );
  await assert.rejects(
    patientRequest("/x", "t", { fetchImpl: async () => new Response("not json", { status: 200 }) }),
    (error) => error.kind === "invalid",
  );
});
