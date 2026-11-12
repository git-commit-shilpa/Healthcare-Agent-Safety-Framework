/**
 * Pure logic for the Patient "Appointments" page. No React, no network.
 * The backend validates, authorizes and re-checks availability on every call; this only
 * shapes the form and the wording. Doctors and time slots are NEVER defined here: they come
 * from the API (which reads the stored physician schedule).
 */

export const MAX_REASON = 500;
export const MAX_REPLY = 1000;
/** Mirrors the booking horizon in migration 013 (the database is the real limit). */
export const BOOKING_HORIZON_DAYS = 60;

// What the patient sees for each appointment status. Internal reasoning is never shown.
export const APPOINTMENT_STATUS = {
  "Pending Confirmation": {
    label: "Pending Technician Confirmation",
    tone: "pending",
    help: "Your request was sent. This time is not booked until a Technician confirms it.",
  },
  "Needs Information": {
    label: "More information needed",
    tone: "escalate",
    help: "Your care team has a question for you. Please reply so they can continue.",
  },
  "Awaiting Physician": {
    label: "Awaiting physician review",
    tone: "escalate",
    help: "A physician is reviewing this request.",
  },
  Confirmed: { label: "Confirmed", tone: "allow", help: "Your appointment is booked." },
  Rejected: { label: "Not approved", tone: "block", help: "This request could not be approved." },
  Cancelled: { label: "Cancelled", tone: "inactive", help: "You cancelled this request." },
  Completed: { label: "Completed", tone: "approved", help: "This appointment is finished." },
};

export function appointmentStatus(status) {
  return APPOINTMENT_STATUS[status] ?? { label: String(status ?? "Unknown"), tone: "neutral", help: "" };
}

export const GROUPS = [
  { key: "upcoming", label: "Upcoming", empty: "You have no confirmed upcoming appointments." },
  { key: "pending", label: "Pending", empty: "You have no pending requests." },
  { key: "completed", label: "Completed", empty: "No completed appointments yet." },
  { key: "rejected", label: "Rejected / cancelled", empty: "No rejected or cancelled requests." },
];

/** Buckets the API items by their server-assigned group. Unknown groups are dropped, not guessed. */
export function groupAppointments(items = []) {
  const grouped = Object.fromEntries(GROUPS.map((g) => [g.key, []]));
  for (const item of items) {
    if (grouped[item.group]) grouped[item.group].push(item);
  }
  // Soonest first for what is coming; newest first for history.
  const byStart = (a, b) => String(a.starts_at).localeCompare(String(b.starts_at));
  grouped.upcoming.sort(byStart);
  grouped.pending.sort((a, b) => Number(b.can_add_info) - Number(a.can_add_info) || byStart(a, b));
  grouped.completed.sort((a, b) => byStart(b, a));
  grouped.rejected.sort((a, b) => byStart(b, a));
  return grouped;
}

export function needsReplyCount(items = []) {
  return items.filter((item) => item.can_add_info).length;
}

/** The default tab: what needs attention first, then what is coming. */
export function defaultGroup(grouped) {
  if (grouped.pending.some((item) => item.can_add_info)) return "pending";
  for (const key of ["upcoming", "pending", "completed", "rejected"]) {
    if (grouped[key].length > 0) return key;
  }
  return "upcoming";
}

// ---- dates ------------------------------------------------------------------------------

function pad(n) {
  return String(n).padStart(2, "0");
}

/** YYYY-MM-DD for a Date, in the browser's local calendar. */
export function toDateInput(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function earliestBookableDate(now = new Date()) {
  return toDateInput(now);
}

export function latestBookableDate(now = new Date()) {
  const limit = new Date(now.getFullYear(), now.getMonth(), now.getDate() + BOOKING_HORIZON_DAYS);
  return toDateInput(limit);
}

/** Returns an error message, or "" when the chosen date can be sent to the API. */
export function validateDate(value, now = new Date()) {
  if (!value) return "Choose a date.";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Enter the date as year-month-day.";
  const [y, m, d] = value.split("-").map(Number);
  const parsed = new Date(y, m - 1, d);
  if (parsed.getFullYear() !== y || parsed.getMonth() !== m - 1 || parsed.getDate() !== d) {
    return "That date does not exist.";
  }
  if (value < earliestBookableDate(now)) return "Choose today or a future date.";
  if (value > latestBookableDate(now)) {
    return `Appointments can be requested up to ${BOOKING_HORIZON_DAYS} days ahead.`;
  }
  return "";
}

// ---- form ---------------------------------------------------------------------------------

export function validateReason(reason) {
  const text = (reason ?? "").trim();
  if (text.length > MAX_REASON) return `Keep this under ${MAX_REASON} characters.`;
  return "";
}

export function validateReply(message) {
  const text = (message ?? "").trim();
  if (!text) return "Write your reply first.";
  if (text.length > MAX_REPLY) return `Keep this under ${MAX_REPLY} characters.`;
  return "";
}

/** What step the booking form is on, given the selections so far. */
export function bookingStep({ physicianId, date, slotStart }, now = new Date()) {
  if (!physicianId) return "doctor";
  if (validateDate(date, now)) return "date";
  if (!slotStart) return "time";
  return "details";
}

/** Only the fields the backend accepts. The requester, status and technician are never sent. */
export function buildBookingPayload({ physicianId, slotStart, reason }) {
  const payload = { physician_id: Number(physicianId), starts_at: slotStart, confirmed: true };
  const text = (reason ?? "").trim();
  if (text) payload.reason = text;
  return payload;
}

/** A selected slot is only valid if it is still in the list the API last returned. */
export function selectionStillAvailable(slots = [], slotStart) {
  return Boolean(slotStart) && slots.some((slot) => slot.start === slotStart);
}

export function findSlot(slots = [], slotStart) {
  return slots.find((slot) => slot.start === slotStart) ?? null;
}

// ---- messages -----------------------------------------------------------------------------

/** Maps an API error to patient-friendly text. `refreshSlots` tells the page to reload times. */
export function describeBookingFailure(error) {
  const code = error?.code ?? null;
  const status = error?.status ?? null;
  const base = { tone: "error", urgent: false, refreshSlots: false, refreshList: false };
  if (status === 504 || error?.kind === "timeout") {
    return {
      ...base,
      tone: "warning",
      refreshList: true,
      message:
        "We could not confirm whether your request was sent. Please check your pending requests below before trying again.",
    };
  }
  switch (code) {
    case "slot_unavailable":
      return {
        ...base,
        tone: "warning",
        refreshSlots: true,
        message: "That time is no longer available. Please choose another time.",
      };
    case "patient_overlap":
      return { ...base, tone: "warning", message: "You already have an appointment request at that time." };
    case "duplicate_request":
      return { ...base, tone: "warning", message: "You have already requested this time." };
    case "too_many_open":
      return {
        ...base,
        tone: "warning",
        message: "You have too many open requests. Please wait for some to be completed.",
      };
    case "emergency_detected":
      return { ...base, urgent: true, message: error.message };
    case "invalid_date":
      return { ...base, refreshSlots: true, message: "Please choose a valid date and time." };
    default:
      return {
        ...base,
        message: error?.message || "Your request could not be sent. Please try again.",
      };
  }
}

export function describeReplyFailure(error) {
  if (error?.code === "emergency_detected") return { urgent: true, message: error.message };
  return { urgent: false, message: error?.message || "Your reply could not be sent. Please try again." };
}

/** Everything the review step shows, so the patient confirms exactly what will be sent. */
export function buildSummary({ physician, slot, reason }) {
  return [
    { label: "Doctor", value: physician?.name ?? "—" },
    { label: "Date and time", value: slot?.local_label ?? "—" },
    ...(physician?.timezone ? [{ label: "Time zone", value: physician.timezone }] : []),
    { label: "Reason", value: (reason ?? "").trim() || "Not provided" },
  ];
}

export function formatCreated(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
