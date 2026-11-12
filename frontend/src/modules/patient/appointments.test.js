import assert from "node:assert/strict";
import test from "node:test";

import {
  APPOINTMENT_STATUS,
  GROUPS,
  MAX_REASON,
  appointmentStatus,
  bookingStep,
  buildBookingPayload,
  buildSummary,
  defaultGroup,
  describeBookingFailure,
  describeReplyFailure,
  earliestBookableDate,
  findSlot,
  groupAppointments,
  latestBookableDate,
  needsReplyCount,
  selectionStillAvailable,
  toDateInput,
  validateDate,
  validateReason,
  validateReply,
} from "./appointments.js";

const NOW = new Date(2026, 10, 6, 10, 0, 0); // 6 Nov 2026, local

test("status labels never claim a booking before confirmation", () => {
  assert.equal(appointmentStatus("Pending Confirmation").label, "Pending Technician Confirmation");
  assert.match(appointmentStatus("Pending Confirmation").help, /not booked/i);
  assert.equal(appointmentStatus("Confirmed").label, "Confirmed");
  for (const key of Object.keys(APPOINTMENT_STATUS)) {
    if (key !== "Confirmed" && key !== "Completed") {
      assert.doesNotMatch(APPOINTMENT_STATUS[key].label, /^(booked|confirmed)$/i, key);
    }
  }
  assert.deepEqual(appointmentStatus("Mystery"), { label: "Mystery", tone: "neutral", help: "" });
});

test("dates: today through the horizon are allowed, everything else is not", () => {
  assert.equal(earliestBookableDate(NOW), "2026-11-06");
  assert.equal(latestBookableDate(NOW), "2027-01-05");
  assert.equal(validateDate("2026-11-06", NOW), "");
  assert.equal(validateDate("2027-01-05", NOW), "");
  assert.match(validateDate("2026-11-05", NOW), /today or a future/i);
  assert.match(validateDate("2027-01-06", NOW), /60 days/);
  assert.match(validateDate("", NOW), /choose a date/i);
  assert.match(validateDate("11/20/2026", NOW), /year-month-day/i);
  assert.match(validateDate("2026-02-30", NOW), /does not exist/i);
  assert.match(validateDate("2026-13-01", NOW), /does not exist/i);
  assert.equal(toDateInput(new Date(2026, 0, 5)), "2026-01-05");
});

test("reason and reply limits", () => {
  assert.equal(validateReason(""), "");
  assert.equal(validateReason("Annual check-up"), "");
  assert.match(validateReason("x".repeat(MAX_REASON + 1)), /under 500/);
  assert.match(validateReply("   "), /write your reply/i);
  assert.equal(validateReply("Yearly check-up"), "");
});

test("the booking step follows the selections", () => {
  assert.equal(bookingStep({ physicianId: "", date: "", slotStart: "" }, NOW), "doctor");
  assert.equal(bookingStep({ physicianId: "4", date: "", slotStart: "" }, NOW), "date");
  assert.equal(bookingStep({ physicianId: "4", date: "2020-01-01", slotStart: "" }, NOW), "date");
  assert.equal(bookingStep({ physicianId: "4", date: "2026-11-20", slotStart: "" }, NOW), "time");
  assert.equal(bookingStep({ physicianId: "4", date: "2026-11-20", slotStart: "2026-11-20T14:00:00Z" }, NOW), "details");
});

test("the booking payload contains only what the API accepts, never a requester or status", () => {
  const payload = buildBookingPayload({ physicianId: "4", slotStart: "2026-11-20T14:00:00Z", reason: "  Check-up  " });
  assert.deepEqual(payload, {
    physician_id: 4,
    starts_at: "2026-11-20T14:00:00Z",
    confirmed: true,
    reason: "Check-up",
  });
  assert.deepEqual(
    buildBookingPayload({ physicianId: 4, slotStart: "2026-11-20T14:00:00Z", reason: "   " }),
    { physician_id: 4, starts_at: "2026-11-20T14:00:00Z", confirmed: true },
  );
  for (const forbidden of ["requested_by", "status", "assigned_to", "task_id"]) {
    assert.equal(forbidden in payload, false);
  }
});

test("a selected slot is only valid while the server still offers it", () => {
  const slots = [{ start: "A", local_label: "x" }, { start: "B", local_label: "y" }];
  assert.equal(selectionStillAvailable(slots, "A"), true);
  assert.equal(selectionStillAvailable(slots, "C"), false);
  assert.equal(selectionStillAvailable(slots, ""), false);
  assert.equal(selectionStillAvailable([], "A"), false);
  assert.deepEqual(findSlot(slots, "B"), slots[1]);
  assert.equal(findSlot(slots, "Z"), null);
});

test("the review summary shows exactly what will be sent", () => {
  const rows = buildSummary({
    physician: { name: "Dr. Fictional", timezone: "America/New_York" },
    slot: { local_label: "Mon, 23 Nov 2026, 9:00 AM EST" },
    reason: "  Annual check-up ",
  });
  assert.deepEqual(rows.map((r) => r.label), ["Doctor", "Date and time", "Time zone", "Reason"]);
  assert.equal(rows[3].value, "Annual check-up");
  assert.equal(buildSummary({ physician: { name: "A" }, slot: { local_label: "t" }, reason: "" }).at(-1).value, "Not provided");
});

test("grouping uses the server group, sorts sensibly and drops unknown groups", () => {
  const items = [
    { request_id: 1, group: "upcoming", starts_at: "2026-12-02T10:00:00Z", can_add_info: false },
    { request_id: 2, group: "upcoming", starts_at: "2026-11-20T10:00:00Z", can_add_info: false },
    { request_id: 3, group: "pending", starts_at: "2026-12-05T10:00:00Z", can_add_info: false },
    { request_id: 4, group: "pending", starts_at: "2026-12-09T10:00:00Z", can_add_info: true },
    { request_id: 5, group: "rejected", starts_at: "2026-10-01T10:00:00Z", can_add_info: false },
    { request_id: 6, group: "surprise", starts_at: "2026-10-01T10:00:00Z", can_add_info: false },
  ];
  const grouped = groupAppointments(items);
  assert.deepEqual(grouped.upcoming.map((i) => i.request_id), [2, 1]);
  assert.deepEqual(grouped.pending.map((i) => i.request_id), [4, 3]); // needs-reply first
  assert.deepEqual(grouped.rejected.map((i) => i.request_id), [5]);
  assert.equal(Object.values(grouped).flat().some((i) => i.request_id === 6), false);
  assert.equal(needsReplyCount(items), 1);
  assert.deepEqual(GROUPS.map((g) => g.key), ["upcoming", "pending", "completed", "rejected"]);
});

test("the default tab surfaces what needs attention", () => {
  const empty = groupAppointments([]);
  assert.equal(defaultGroup(empty), "upcoming");
  const needsReply = groupAppointments([{ request_id: 1, group: "pending", can_add_info: true, starts_at: "a" }]);
  assert.equal(defaultGroup(needsReply), "pending");
  const onlyHistory = groupAppointments([{ request_id: 1, group: "completed", can_add_info: false, starts_at: "a" }]);
  assert.equal(defaultGroup(onlyHistory), "completed");
  const both = groupAppointments([
    { request_id: 1, group: "upcoming", can_add_info: false, starts_at: "a" },
    { request_id: 2, group: "pending", can_add_info: true, starts_at: "b" },
  ]);
  assert.equal(defaultGroup(both), "pending");
});

test("booking failures become patient-friendly guidance", () => {
  const conflict = describeBookingFailure({ status: 409, code: "slot_unavailable", message: "x" });
  assert.equal(conflict.refreshSlots, true);
  assert.match(conflict.message, /no longer available/i);
  assert.equal(describeBookingFailure({ status: 409, code: "patient_overlap" }).tone, "warning");
  assert.match(describeBookingFailure({ status: 409, code: "duplicate_request" }).message, /already requested/i);
  assert.match(describeBookingFailure({ status: 429, code: "too_many_open" }).message, /too many/i);

  const unknown = describeBookingFailure({ status: 504, kind: "http" });
  assert.equal(unknown.refreshList, true);
  assert.match(unknown.message, /could not confirm/i);
  assert.equal(describeBookingFailure({ kind: "timeout" }).refreshList, true);

  const emergency = describeBookingFailure({ status: 422, code: "emergency_detected", message: "Call emergency services." });
  assert.equal(emergency.urgent, true);
  assert.equal(emergency.message, "Call emergency services.");

  assert.match(describeBookingFailure(undefined).message, /could not be sent/i);
  assert.equal(describeReplyFailure({ code: "emergency_detected", message: "Call now." }).urgent, true);
  assert.equal(describeReplyFailure({ message: "Nope" }).urgent, false);
});
