import assert from "node:assert/strict";
import test from "node:test";

import {
  EMPTY_REQUEST_FORM,
  buildCreatePayload,
  describeSubmitFailure,
  needsReplyCount,
  patientStatus,
  sortRequests,
  validateInfoMessage,
  validateRequestForm,
} from "./serviceRequests.js";

const categories = [{ value: "general_inquiry" }, { value: "medication_request" }];
const form = (o) => ({ ...EMPTY_REQUEST_FORM, ...o });

test("a request needs a category and a title; the description is optional", () => {
  assert.deepEqual(Object.keys(validateRequestForm(form({}), categories).errors).sort(), ["category", "title"]);
  assert.equal(validateRequestForm(form({ category: "general_inquiry", title: "Gloves" }), categories).valid, true);
  assert.equal(validateRequestForm(form({ category: "general_inquiry", title: "   " }), categories).errors.title !== undefined, true);
  assert.equal(validateRequestForm(form({ category: "bogus", title: "x" }), categories).errors.category !== undefined, true);
  assert.equal(validateRequestForm(form({ category: "general_inquiry", title: "x".repeat(201) }), categories).errors.title !== undefined, true);
  assert.equal(validateRequestForm(form({ category: "general_inquiry", title: "x", description: "d".repeat(1001) }), categories).errors.description !== undefined, true);
});

test("the payload has only the accepted fields and never a requester, status or assignee", () => {
  assert.deepEqual(buildCreatePayload(form({ category: "general_inquiry", title: "  Gloves  ", description: " " })), { category: "general_inquiry", title: "Gloves" });
  assert.deepEqual(
    buildCreatePayload(form({ category: "medication_request", title: "Refill", description: " please " })),
    { category: "medication_request", title: "Refill", description: "please" },
  );
});

test("replying to an on-hold request needs text within the limit", () => {
  assert.notEqual(validateInfoMessage("   "), null);
  assert.notEqual(validateInfoMessage("x".repeat(1001)), null);
  assert.equal(validateInfoMessage(" 1-3 May "), null);
});

test("patients see friendly statuses and never internal wording", () => {
  for (const status of ["Submitted", "In Review", "On Hold", "Awaiting Physician", "In Progress", "Completed", "Declined", "Cancelled"]) {
    const meta = patientStatus(status);
    assert.ok(meta.label && meta.help, status);
    assert.doesNotMatch(`${meta.label} ${meta.help}`, /ALLOW|BLOCK|HOLD|escalat|guardrail|technician/i, status);
  }
  assert.equal(patientStatus("Weird").tone, "neutral");
});

test("requests that need a reply are listed first", () => {
  const items = [
    { task_id: 1, can_cancel: false, can_add_info: false },
    { task_id: 2, can_cancel: true, can_add_info: false },
    { task_id: 3, can_cancel: true, can_add_info: true },
  ];
  assert.deepEqual(sortRequests(items).map((i) => i.task_id), [3, 2, 1]);
  assert.equal(needsReplyCount(items), 1);
});

test("submission failures: an emergency is urgent, duplicates and no technician are warnings", () => {
  assert.deepEqual(describeSubmitFailure({ code: "emergency_detected", message: "Call 911." }), { tone: "error", urgent: true, message: "Call 911." });
  assert.equal(describeSubmitFailure({ code: "duplicate_request", message: "Already open." }).tone, "warning");
  assert.equal(describeSubmitFailure({ code: "no_eligible_technician", message: "Contact your care team." }).tone, "warning");
  assert.equal(describeSubmitFailure({ status: 504, message: "Check first." }).tone, "warning");
  assert.equal(describeSubmitFailure(new Error("")).message, "Your request could not be submitted.");
});
