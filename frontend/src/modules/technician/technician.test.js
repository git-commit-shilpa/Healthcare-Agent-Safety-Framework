import assert from "node:assert/strict";
import test from "node:test";

import {
  CATEGORY_LABELS,
  EMPTY_DECISION_FORM,
  allowWarning,
  availableDecisions,
  buildDecisionPayload,
  canComplete,
  canStartReview,
  describeDecisionResult,
  eventLabel,
  filterTasks,
  groupCounts,
  needsPhysician,
  statusMeta,
  validateDecisionForm,
} from "./technician.js";

const form = (overrides) => ({ ...EMPTY_DECISION_FORM, ...overrides });

test("every backend category and status has a label", () => {
  assert.equal(Object.keys(CATEGORY_LABELS).length, 10); // nine request categories + record_correction (migration 014)
  for (const status of ["Submitted", "In Review", "On Hold", "Awaiting Physician", "In Progress", "Completed", "Declined", "Cancelled"]) {
    const meta = statusMeta(status);
    assert.ok(meta.label.length > 0 && meta.tone.length > 0, status);
    assert.notEqual(meta.label, "Unknown", status);
  }
  assert.deepEqual(statusMeta("Mystery"), { label: "Mystery", tone: "neutral" });
});

test("task groups filter by status and count correctly", () => {
  const items = [
    { status: "Submitted" }, { status: "In Review" }, { status: "In Progress" },
    { status: "On Hold" }, { status: "Awaiting Physician" }, { status: "Completed" }, { status: "Declined" },
  ];
  assert.equal(filterTasks(items, "needs_review").length, 2);
  assert.equal(filterTasks(items, "waiting").length, 2);
  assert.equal(filterTasks(items, "closed").length, 2);
  assert.equal(filterTasks(items, "all").length, 7);
  assert.deepEqual(groupCounts(items), { needs_review: 2, in_progress: 1, waiting: 2, closed: 2, all: 7 });
});

test("available decisions mirror the backend state machine", () => {
  assert.deepEqual(availableDecisions({ status: "Submitted" }), []);
  assert.deepEqual(availableDecisions({ status: "In Review" }), ["ALLOW", "BLOCK", "HOLD"]);
  assert.deepEqual(availableDecisions({ status: "On Hold" }), ["BLOCK"]);
  assert.deepEqual(availableDecisions({ status: "In Progress" }), []);
  assert.deepEqual(availableDecisions({ status: "Completed" }), []);
  assert.deepEqual(availableDecisions({ status: "Awaiting Physician", approval_status: "pending" }), ["ALLOW"]);
  assert.deepEqual(availableDecisions({ status: "Awaiting Physician", approval_status: "approved" }), ["ALLOW", "BLOCK"]);
  assert.deepEqual(availableDecisions({ status: "Awaiting Physician", approval_status: "rejected" }), ["ALLOW", "BLOCK"]);
});

test("review, complete and physician-need helpers", () => {
  assert.equal(canStartReview({ status: "Submitted" }), true);
  assert.equal(canStartReview({ status: "On Hold" }), true);
  assert.equal(canStartReview({ status: "In Review" }), false);
  assert.equal(canComplete({ status: "In Progress" }), true);
  assert.equal(canComplete({ status: "In Review" }), false);
  assert.equal(needsPhysician({ requires_physician_approval: true, status: "In Review" }), true);
  assert.equal(needsPhysician({ requires_physician_approval: false, status: "Awaiting Physician" }), true);
  assert.equal(needsPhysician({ requires_physician_approval: false, status: "In Review" }), false);
});

test("the ALLOW warning appears whenever a granted physician approval is missing", () => {
  assert.equal(allowWarning({ requires_physician_approval: false, status: "In Review", approval_status: "none" }), null);
  assert.match(allowWarning({ requires_physician_approval: true, status: "In Review", approval_status: "none" }), /HOLD/);
  assert.match(allowWarning({ requires_physician_approval: true, status: "Awaiting Physician", approval_status: "pending" }), /approval/);
  assert.match(allowWarning({ requires_physician_approval: true, status: "Awaiting Physician", approval_status: "rejected" }), /declined/);
  assert.match(allowWarning({ requires_physician_approval: true, status: "Awaiting Physician", approval_status: "invalid" }), /not valid/);
  assert.equal(allowWarning({ requires_physician_approval: true, status: "Awaiting Physician", approval_status: "approved" }), null);
});

test("BLOCK and HOLD require an internal reason and a patient message; ALLOW does not", () => {
  assert.equal(validateDecisionForm(form({ decision: "ALLOW" })).valid, true);
  assert.equal(validateDecisionForm(form({})).errors.decision !== undefined, true);
  const block = validateDecisionForm(form({ decision: "BLOCK" }));
  assert.deepEqual(Object.keys(block.errors).sort(), ["internalNote", "patientMessage"]);
  assert.equal(validateDecisionForm(form({ decision: "BLOCK", internalNote: "  ", patientMessage: "m" })).errors.internalNote !== undefined, true);
  assert.equal(validateDecisionForm(form({ decision: "BLOCK", internalNote: "n", patientMessage: "m" })).valid, true);
  const hold = validateDecisionForm(form({ decision: "HOLD", internalNote: "n", patientMessage: "m" }));
  assert.deepEqual(Object.keys(hold.errors), ["holdReason"]);
  assert.equal(validateDecisionForm(form({ decision: "HOLD", holdReason: "physician_approval", internalNote: "n", patientMessage: "m" })).valid, true);
  assert.equal(validateDecisionForm(form({ decision: "ALLOW", internalNote: "x".repeat(2001) })).errors.internalNote !== undefined, true);
  assert.equal(validateDecisionForm(form({ decision: "ALLOW", patientMessage: "x".repeat(501) })).errors.patientMessage !== undefined, true);
});

test("the payload contains only accepted fields, trimmed, and no actor or status", () => {
  assert.deepEqual(buildDecisionPayload(form({ decision: "ALLOW" })), { decision: "ALLOW" });
  assert.deepEqual(
    buildDecisionPayload(form({ decision: "HOLD", holdReason: "needs_information", internalNote: " why ", patientMessage: " tell us " })),
    { decision: "HOLD", hold_reason: "needs_information", internal_note: "why", patient_message: "tell us" },
  );
  // A stale hold reason must not leak into a BLOCK.
  assert.deepEqual(
    buildDecisionPayload(form({ decision: "BLOCK", holdReason: "physician_approval", internalNote: "n", patientMessage: "m" })),
    { decision: "BLOCK", internal_note: "n", patient_message: "m" },
  );
});

test("decision results are explained, and an override is never shown as success", () => {
  assert.equal(describeDecisionResult({ outcome: "executed", explanation: "ok" }).tone, "success");
  assert.equal(describeDecisionResult({ outcome: "approval_requested" }).tone, "info");
  const escalated = describeDecisionResult({ outcome: "escalated", explanation: "needs approval" });
  assert.deepEqual([escalated.tone, escalated.message], ["warning", "needs approval"]);
  assert.equal(describeDecisionResult({ outcome: "rejected", explanation: "no" }).tone, "error");
  assert.equal(describeDecisionResult({}).tone, "error");
});

test("history labels", () => {
  assert.equal(eventLabel({ event_type: "decision", decision: "HOLD", hold_reason: "needs_information" }), "Decision: HOLD (needs information)");
  assert.equal(eventLabel({ event_type: "decision_rejected", decision: "ALLOW" }), "Decision not applied: ALLOW");
  assert.equal(eventLabel({ event_type: "info_provided" }), "Patient added information");
  assert.equal(eventLabel({ event_type: "whatever" }), "Update");
});

test("scopeTasks keeps appointment requests out of Tasks and out of the way of everything else", async () => {
  const { scopeTasks } = await import("./technician.js");
  const items = [
    { task_id: 1, category: "appointment_scheduling" },
    { task_id: 2, category: "general_inquiry" },
    { task_id: 3, category: "medication_request" },
  ];
  assert.deepEqual(scopeTasks(items, "tasks", true).map((t) => t.task_id), [2, 3]);
  assert.deepEqual(scopeTasks(items, "scheduling", true).map((t) => t.task_id), [1]);
  // Appointments off: Tasks shows everything (unchanged behaviour) and there is nothing to schedule.
  assert.deepEqual(scopeTasks(items, "tasks", false).map((t) => t.task_id), [1, 2, 3]);
  assert.deepEqual(scopeTasks(items, "scheduling", false), []);
});
