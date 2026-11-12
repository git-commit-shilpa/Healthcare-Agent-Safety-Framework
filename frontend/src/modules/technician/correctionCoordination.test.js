import assert from "node:assert/strict";
import test from "node:test";

import {
  CORRECTION_ACTIONS,
  availableCorrectionActions,
  buildCorrectionPayload,
  correctionWaitingNote,
  describeCorrectionResult,
  isCorrectionTask,
  validateCorrectionForm,
} from "./correctionCoordination.js";
import { APPROVAL_META, CATEGORY_LABELS, eventLabel, scopeTasks } from "./technician.js";

const REASONS = [
  { code: "duplicate_request", label: "Duplicate" },
  { code: "withdrawn_by_patient", label: "Withdrawn" },
];
const task = (status, approval, actions) => ({
  category: "record_correction",
  status,
  correction: { request_id: 5, approval_status: approval, reviewed_at: null, allowed_actions: actions, decline_reasons: REASONS },
});

test("the only actions that exist are HOLD, DENY and close; there is no ALLOW or approve/reject", () => {
  assert.deepEqual(Object.keys(CORRECTION_ACTIONS), ["request_physician_authorization", "request_information", "decline", "close"]);
  assert.deepEqual(Object.values(CORRECTION_ACTIONS).map((a) => a.kind), ["HOLD", "HOLD", "DENY", "CLOSE"]);
  assert.doesNotMatch(JSON.stringify(CORRECTION_ACTIONS), /ALLOW|approve this|reject this/i);
});

test("available actions mirror what the backend allows, in a stable order", () => {
  assert.deepEqual(
    availableCorrectionActions(task("In Review", "not_requested", ["decline", "request_information", "request_physician_authorization"])).map((a) => a.key),
    ["request_physician_authorization", "request_information", "decline"],
  );
  assert.deepEqual(availableCorrectionActions(task("Submitted", "not_requested", [])), []);
  assert.deepEqual(availableCorrectionActions({}), []);
});

test("authorization and information requests need an internal note; the question needs real content", () => {
  assert.equal(validateCorrectionForm({ action: "request_physician_authorization", reasonCode: "", internalNote: "Complete.", patientMessage: "" }).valid, true);
  assert.equal(validateCorrectionForm({ action: "request_physician_authorization", reasonCode: "", internalNote: "  ", patientMessage: "" }).valid, false);
  const q = validateCorrectionForm({ action: "request_information", reasonCode: "", internalNote: "n", patientMessage: "Why?" });
  assert.equal(q.valid, false);
  assert.match(q.errors.patientMessage, /at least 10/);
  assert.equal(validateCorrectionForm({ action: "request_information", reasonCode: "", internalNote: "n", patientMessage: "Which date did this change?" }).valid, true);
  assert.equal(validateCorrectionForm({ action: "close", reasonCode: "", internalNote: "", patientMessage: "" }).valid, true);
  assert.equal(validateCorrectionForm({ action: "", reasonCode: "", internalNote: "", patientMessage: "" }).valid, false);
});

test("DENY needs a standard administrative reason, an internal note and a real explanation", () => {
  const base = { action: "decline", reasonCode: "duplicate_request", internalNote: "Same as before.", patientMessage: "We already have this request on file." };
  assert.equal(validateCorrectionForm(base, REASONS).valid, true);
  assert.match(validateCorrectionForm({ ...base, reasonCode: "" }, REASONS).errors.reasonCode, /standard administrative/);
  assert.match(validateCorrectionForm({ ...base, reasonCode: "clinically_unsafe" }, REASONS).errors.reasonCode, /standard/);
  assert.match(validateCorrectionForm({ ...base, patientMessage: "Too short." }, REASONS).errors.patientMessage, /at least 20/);
  assert.equal(validateCorrectionForm({ ...base, internalNote: "" }, REASONS).valid, false);
  assert.equal(validateCorrectionForm(base, []).valid, false);                       // no reasons offered by the server -> nothing to choose
});

test("the payload carries the action, the reason (only for DENY) and trimmed text only", () => {
  assert.deepEqual(buildCorrectionPayload({ action: "request_information", reasonCode: "x", internalNote: " n ", patientMessage: " Which date? " }),
    { action: "request_information", internal_note: "n", patient_message: "Which date?" });
  assert.deepEqual(buildCorrectionPayload({ action: "decline", reasonCode: "duplicate_request", internalNote: "n", patientMessage: "We already have this request." }),
    { action: "decline", reason_code: "duplicate_request", internal_note: "n", patient_message: "We already have this request." });
  assert.deepEqual(buildCorrectionPayload({ action: "close", reasonCode: "", internalNote: "", patientMessage: "" }), { action: "close" });
});

test("a task is a correction only when it has the category and the coordination block", () => {
  assert.equal(isCorrectionTask(task("In Review", "pending", [])), true);
  assert.equal(isCorrectionTask({ category: "record_correction" }), false);
  assert.equal(isCorrectionTask({ category: "general_inquiry", correction: {} }), false);
});

test("waiting notes explain why nothing can be done", () => {
  assert.match(correctionWaitingNote(task("Submitted", "not_requested", [])), /Start the review/);
  assert.match(correctionWaitingNote(task("On Hold", "not_requested", [])), /patient/);
  assert.match(correctionWaitingNote(task("Awaiting Physician", "invalid", [])), /could not be verified/);
  assert.match(correctionWaitingNote(task("Completed", "approved", [])), /closed/);
  assert.match(correctionWaitingNote(task("Declined", "not_requested", [])), /declined/);
});

test("results say when an existing approval was reused", () => {
  assert.match(describeCorrectionResult({ message: "Sent.", outcome: "approval_reused" }).message, /no second one was created/);
  assert.doesNotMatch(describeCorrectionResult({ message: "Sent.", outcome: "approval_requested" }).message, /second/);
});

test("labels exist for the new category, the not-yet-requested approval and the coordination history", () => {
  assert.equal(CATEGORY_LABELS.record_correction, "Record correction");
  assert.equal(APPROVAL_META.not_requested.label, "Not sent to the Physician yet");
  assert.match(eventLabel({ event_type: "coordination_info_requested" }), /information/i);
  assert.match(eventLabel({ event_type: "coordination_closed" }), /closed/i);
  assert.match(eventLabel({ event_type: "decision", decision: "HOLD", hold_reason: "physician_approval" }), /HOLD/);
});

test("corrections stay in Tasks and out of Scheduling Requests", () => {
  const items = [{ task_id: 1, category: "appointment_scheduling" }, { task_id: 2, category: "record_correction" }];
  assert.deepEqual(scopeTasks(items, "tasks", true).map((t) => t.task_id), [2]);
  assert.deepEqual(scopeTasks(items, "scheduling", true).map((t) => t.task_id), [1]);
});
