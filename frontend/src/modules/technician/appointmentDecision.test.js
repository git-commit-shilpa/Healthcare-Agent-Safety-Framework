import assert from "node:assert/strict";
import test from "node:test";

import {
  APPOINTMENT_ACTIONS,
  EMPTY_APPOINTMENT_FORM,
  appointmentEventLabel,
  availableAppointmentActions,
  buildAppointmentPayload,
  defaultConfirmMessage,
  describeAppointmentFailure,
  describeAppointmentResult,
  internalDecisionLabel,
  shouldCheckSlot,
  slotCheckView,
  validateAppointmentForm,
} from "./appointmentDecision.js";

const appointment = { physician_name: "Dr. Fictional", local_label: "Mon, 23 Nov 2026, 9:00 AM EST" };
const inReview = { status: "In Review", approval_status: "none", appointment };
const form = (action, internalNote = "", patientMessage = "") => ({ ...EMPTY_APPOINTMENT_FORM, action, internalNote, patientMessage });

test("every action maps to one internal ALLOW / BLOCK / HOLD guardrail decision", () => {
  const map = Object.fromEntries(Object.values(APPOINTMENT_ACTIONS).map((a) => [a.title, [a.decision, a.holdReason]]));
  assert.deepEqual(map, {
    "Confirm Appointment": ["ALLOW", null],
    "Reject Request": ["BLOCK", null],
    "Request More Information": ["HOLD", "needs_information"],
    "Refer to Physician": ["HOLD", "physician_approval"],
  });
  assert.equal(internalDecisionLabel("confirm"), "ALLOW");
  assert.equal(internalDecisionLabel("request_info"), "HOLD (needs information)");
  assert.equal(internalDecisionLabel("refer"), "HOLD (physician approval)");
  assert.equal(internalDecisionLabel("nope"), "");
});

test("the buttons never use the generic wording", () => {
  for (const action of Object.values(APPOINTMENT_ACTIONS)) {
    for (const text of [action.title, action.description, action.submit]) {
      assert.doesNotMatch(text, /\b(ALLOW|BLOCK|HOLD)\b/);
      assert.doesNotMatch(text, /pending until something else/i);
    }
  }
});

test("only actions the backend would accept are offered", () => {
  const keys = (task) => availableAppointmentActions(task).map((a) => a.key);
  assert.deepEqual(keys(inReview), ["confirm", "request_info", "reject", "refer"]);
  assert.deepEqual(keys({ ...inReview, status: "On Hold" }), ["reject"]);
  assert.deepEqual(keys({ ...inReview, status: "Awaiting Physician", approval_status: "pending" }), ["confirm"]);
  assert.deepEqual(keys({ ...inReview, status: "Awaiting Physician", approval_status: "approved" }), ["confirm", "reject"]);
  for (const status of ["Submitted", "In Progress", "Completed", "Declined", "Cancelled"]) {
    assert.deepEqual(keys({ ...inReview, status }), [], status);
  }
});

test("minimum lengths match the backend and database rules", () => {
  assert.equal(validateAppointmentForm(form("confirm", "", "Confirmed for you.")).valid, true);
  assert.match(validateAppointmentForm(form("confirm", "", "ok")).errors.patientMessage, /at least 10/);
  assert.match(validateAppointmentForm(form("confirm", "", "")).errors.patientMessage, /required/);

  assert.match(validateAppointmentForm(form("reject", "", "x".repeat(25))).errors.internalNote, /required/);
  assert.match(validateAppointmentForm(form("reject", "n", "No.")).errors.patientMessage, /at least 20/);
  assert.match(validateAppointmentForm(form("reject", "n", "x".repeat(19))).errors.patientMessage, /at least 20/);
  assert.equal(validateAppointmentForm(form("reject", "n", "x".repeat(20))).valid, true);

  assert.match(validateAppointmentForm(form("request_info", "n", "Why?")).errors.patientMessage, /at least 10/);
  assert.equal(validateAppointmentForm(form("request_info", "n", "What is the visit for?")).valid, true);
  assert.match(validateAppointmentForm(form("request_info", "", "What is the visit for?")).errors.internalNote, /required/);

  assert.equal(validateAppointmentForm(form("refer", "Needs clinical review", "A physician is reviewing.")).valid, true);
  assert.equal(validateAppointmentForm(form("refer", "", "A physician is reviewing.")).valid, false);

  assert.match(validateAppointmentForm(EMPTY_APPOINTMENT_FORM).errors.action, /choose/i);
  assert.match(validateAppointmentForm(form("confirm", "n".repeat(2001), "Confirmed for you.")).errors.internalNote, /under 2000/);
  assert.match(validateAppointmentForm(form("confirm", "", "m".repeat(501))).errors.patientMessage, /under 500/);
});

test("whitespace-only text does not satisfy a required field", () => {
  assert.equal(validateAppointmentForm(form("reject", "   ", "          \n          ")).valid, false);
});

test("payloads contain only backend fields and map the action to the decision", () => {
  assert.deepEqual(buildAppointmentPayload(form("confirm", "", "  Confirmed for you.  ")), {
    decision: "ALLOW",
    patient_message: "Confirmed for you.",
  });
  assert.deepEqual(buildAppointmentPayload(form("reject", " n ", "x".repeat(20))), {
    decision: "BLOCK",
    internal_note: "n",
    patient_message: "x".repeat(20),
  });
  assert.deepEqual(buildAppointmentPayload(form("request_info", "n", "What is the visit for?")), {
    decision: "HOLD",
    hold_reason: "needs_information",
    internal_note: "n",
    patient_message: "What is the visit for?",
  });
  assert.equal(buildAppointmentPayload(form("refer", "n", "A physician is reviewing.")).hold_reason, "physician_approval");
  const payload = buildAppointmentPayload(form("confirm", "", "Confirmed for you."));
  for (const forbidden of ["status", "task_id", "assigned_to", "guardrail_decision", "action"]) {
    assert.equal(forbidden in payload, false, forbidden);
  }
});

test("the confirmation message is pre-filled with doctor and local time", () => {
  assert.equal(
    defaultConfirmMessage(appointment),
    "Your appointment with Dr. Fictional on Mon, 23 Nov 2026, 9:00 AM EST is confirmed.",
  );
  assert.match(defaultConfirmMessage({ local_label: "x" }), /your doctor/);
  assert.equal(defaultConfirmMessage(null), "");
});

test("the availability check only applies to appointment requests that can still be confirmed", () => {
  assert.equal(shouldCheckSlot(inReview), true);
  assert.equal(shouldCheckSlot({ ...inReview, status: "Awaiting Physician" }), true);
  for (const status of ["Submitted", "On Hold", "In Progress", "Completed"]) {
    assert.equal(shouldCheckSlot({ ...inReview, status }), false, status);
  }
  assert.equal(shouldCheckSlot({ status: "In Review", appointment: null }), false);
  assert.equal(shouldCheckSlot(null), false);
});

test("an unavailable slot blocks Confirm but never Reject or Request More Information", () => {
  assert.deepEqual(slotCheckView(null), { tone: "info", blocksConfirm: false, message: "" });
  assert.equal(slotCheckView({ result: "ok", message: "Free" }).blocksConfirm, false);
  assert.equal(slotCheckView({ result: "ok", message: "Free" }).tone, "success");
  for (const result of ["slot_unavailable", "physician_unavailable", "already_confirmed", "not_pending"]) {
    assert.equal(slotCheckView({ result, message: "m" }).blocksConfirm, true, result);
  }
  assert.equal(slotCheckView({ result: "slot_unavailable", message: "m" }).tone, "warning");
  assert.equal(slotCheckView({ result: "surprise", message: "m" }).blocksConfirm, false);
});

test("results use appointment wording and keep the internal decision visible", () => {
  const confirmed = describeAppointmentResult(
    { outcome: "executed", explanation: "Appointment confirmed.", guardrail_decision: "Allow" }, "confirm");
  assert.deepEqual(
    [confirmed.title, confirmed.tone, confirmed.decision, confirmed.guardrail, confirmed.recordedAs],
    ["Appointment confirmed", "success", "ALLOW", "Allow", "ALLOW"],
  );
  assert.equal(describeAppointmentResult({ outcome: "recorded", explanation: "x" }, "reject").title, "Request rejected");
  assert.equal(describeAppointmentResult({ outcome: "recorded", explanation: "x" }, "request_info").title, "Question sent");
  assert.equal(describeAppointmentResult({ outcome: "approval_requested" }, "refer").title, "Referred to physician");
  assert.equal(describeAppointmentResult({ outcome: "rejected", explanation: "no" }, "confirm").tone, "error");
  assert.equal(describeAppointmentResult({ outcome: "escalated", explanation: "e" }, "confirm").tone, "warning");
  assert.equal(describeAppointmentResult({ outcome: "???" }, "confirm").tone, "error");
});

test("a lost race explains what to do next and triggers a re-check", () => {
  const conflict = describeAppointmentFailure({ code: "slot_unavailable", status: 409, message: "x" });
  assert.equal(conflict.recheck, true);
  assert.match(conflict.message, /nothing was changed/i);
  assert.match(conflict.message, /reject the request/i);
  const unknown = describeAppointmentFailure({ status: 504, message: "We could not confirm." });
  assert.equal(unknown.recheck, true);
  assert.match(unknown.message, /check its status/i);
  assert.deepEqual(describeAppointmentFailure({ code: "rejection_reason_too_short", message: "Too short." }),
    { recheck: false, message: "Too short." });
  assert.match(describeAppointmentFailure(undefined).message, /could not be completed/i);
});

test("history wording for appointments keeps the audit decision in brackets", () => {
  assert.equal(appointmentEventLabel({ event_type: "decision", decision: "ALLOW" }), "Appointment confirmed (ALLOW)");
  assert.equal(appointmentEventLabel({ event_type: "decision", decision: "BLOCK" }), "Request rejected (BLOCK)");
  assert.equal(
    appointmentEventLabel({ event_type: "decision", decision: "HOLD", hold_reason: "needs_information" }),
    "More information requested (HOLD)");
  assert.equal(
    appointmentEventLabel({ event_type: "decision", decision: "HOLD", hold_reason: "physician_approval" }),
    "Referred to physician (HOLD)");
  assert.equal(appointmentEventLabel({ event_type: "info_provided" }), "Patient responded");
  assert.equal(appointmentEventLabel({ event_type: "created" }), null);
});
