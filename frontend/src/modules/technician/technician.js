/**
 * Pure Technician-portal logic. No React, no network, so it is unit-testable.
 * The backend is the authority on every rule below; these only shape the UI and
 * keep the Technician from sending requests that cannot succeed.
 */

export const MAX_INTERNAL_NOTE = 2000;
export const MAX_PATIENT_MESSAGE = 500;

export const CATEGORY_LABELS = {
  appointment_scheduling: "Appointment scheduling",
  test_result_upload: "Test result upload",
  sample_collection: "Sample collection",
  supplies_equipment: "Supplies or equipment",
  documentation_request: "Documentation request",
  general_inquiry: "General question",
  medication_request: "Medication request",
  treatment_change_request: "Treatment change request",
  diagnosis_inquiry: "Diagnosis question",
  record_correction: "Record correction",
};

export const STATUS_META = {
  Submitted: { label: "New", tone: "pending" },
  "In Review": { label: "In review", tone: "pending" },
  "On Hold": { label: "On hold", tone: "neutral" },
  "Awaiting Physician": { label: "Awaiting physician", tone: "escalate" },
  "In Progress": { label: "In progress", tone: "allow" },
  Completed: { label: "Completed", tone: "approved" },
  Declined: { label: "Declined", tone: "block" },
  Cancelled: { label: "Cancelled", tone: "inactive" },
};

export const APPROVAL_META = {
  none: { label: "No approval needed", tone: "neutral" },
  not_requested: { label: "Not sent to the Physician yet", tone: "neutral" },
  pending: { label: "Physician approval pending", tone: "pending" },
  approved: { label: "Approved by physician", tone: "approved" },
  rejected: { label: "Rejected by physician", tone: "rejected" },
  invalid: { label: "Approval not valid", tone: "block" },
};

export const TASK_GROUPS = [
  { id: "needs_review", label: "Needs review", statuses: ["Submitted", "In Review"] },
  { id: "in_progress", label: "In progress", statuses: ["In Progress"] },
  { id: "waiting", label: "On hold / awaiting", statuses: ["On Hold", "Awaiting Physician"] },
  { id: "closed", label: "Closed", statuses: ["Completed", "Declined", "Cancelled"] },
  { id: "all", label: "All", statuses: null },
];

export function statusMeta(status) {
  return STATUS_META[status] ?? { label: String(status ?? "Unknown"), tone: "neutral" };
}

export function categoryLabel(category) {
  return CATEGORY_LABELS[category] ?? String(category ?? "Request");
}

export function filterTasks(items, groupId) {
  const group = TASK_GROUPS.find((g) => g.id === groupId) ?? TASK_GROUPS[0];
  return group.statuses === null ? items : items.filter((t) => group.statuses.includes(t.status));
}

export function groupCounts(items) {
  return Object.fromEntries(TASK_GROUPS.map((g) => [g.id, filterTasks(items, g.id).length]));
}

/** A task that must not proceed without a granted physician approval. */
export function needsPhysician(task) {
  return Boolean(task.requires_physician_approval) || task.status === "Awaiting Physician";
}

export function canStartReview(task) {
  return task.status === "Submitted" || task.status === "On Hold";
}

export function canComplete(task) {
  return task.status === "In Progress";
}

/** Decisions the backend would accept from the task's current status. */
export function availableDecisions(task) {
  const decisions = [];
  if (["In Review", "Awaiting Physician"].includes(task.status)) decisions.push("ALLOW");
  if (["In Review", "On Hold", "Awaiting Physician"].includes(task.status)) {
    // The backend refuses to decline while a physician approval is still pending.
    const blocked = task.status === "Awaiting Physician" && task.approval_status === "pending";
    if (!blocked) decisions.push("BLOCK");
  }
  if (task.status === "In Review") decisions.push("HOLD");
  return decisions;
}

/** Why ALLOW would not take effect right now (the backend decides; this only warns). */
export function allowWarning(task) {
  if (!needsPhysician(task) || task.approval_status === "approved") return null;
  if (task.approval_status === "rejected") {
    return "The physician rejected this request. It can only be declined.";
  }
  if (task.approval_status === "invalid") {
    return "The linked approval is not valid, so this request cannot proceed.";
  }
  return "This request needs a physician approval before it can proceed. Choose HOLD and request physician approval.";
}

export const EMPTY_DECISION_FORM = Object.freeze({
  decision: "",
  holdReason: "",
  internalNote: "",
  patientMessage: "",
});

export function validateDecisionForm(form) {
  const errors = {};
  const note = form.internalNote.trim();
  const message = form.patientMessage.trim();
  if (!["ALLOW", "BLOCK", "HOLD"].includes(form.decision)) errors.decision = "Choose ALLOW, BLOCK or HOLD.";
  if (form.decision === "HOLD" && !["needs_information", "physician_approval"].includes(form.holdReason)) {
    errors.holdReason = "Choose why the request is on hold.";
  }
  if (form.decision === "BLOCK" || form.decision === "HOLD") {
    if (!note) errors.internalNote = "An internal reason is required.";
    if (!message) errors.patientMessage = "A message for the patient is required.";
  }
  if (note.length > MAX_INTERNAL_NOTE) errors.internalNote = `Keep this under ${MAX_INTERNAL_NOTE} characters.`;
  if (message.length > MAX_PATIENT_MESSAGE) errors.patientMessage = `Keep this under ${MAX_PATIENT_MESSAGE} characters.`;
  return { errors, valid: Object.keys(errors).length === 0 };
}

/** The exact request body: only the fields the backend accepts, trimmed, no extras. */
export function buildDecisionPayload(form) {
  const payload = { decision: form.decision };
  if (form.decision === "HOLD") payload.hold_reason = form.holdReason;
  const note = form.internalNote.trim();
  const message = form.patientMessage.trim();
  if (note) payload.internal_note = note;
  if (message) payload.patient_message = message;
  return payload;
}

/** Turns the backend decision response into something the Technician can read. */
export function describeDecisionResult(result) {
  switch (result?.outcome) {
    case "executed":
    case "recorded":
      return { tone: "success", title: "Decision recorded", message: result.explanation };
    case "approval_requested":
      return {
        tone: "info",
        title: "Physician approval requested",
        message: "The request is waiting for the assigned physician. You will see the result here.",
      };
    case "approval_reused":
      return {
        tone: "info",
        title: "Approval already requested",
        message: "A physician approval is already pending for this request.",
      };
    case "escalated":
      return {
        tone: "warning",
        title: "Not applied: physician approval needed",
        message: result.explanation,
      };
    case "rejected":
      return { tone: "error", title: "Not applied", message: result.explanation };
    default:
      return { tone: "error", title: "Unexpected response", message: "Please refresh and check the request." };
  }
}

export function formatDateTime(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

const EVENT_LABELS = {
  created: "Request submitted",
  review_started: "Review started",
  review_resumed: "Review resumed",
  decision: "Decision",
  decision_rejected: "Decision not applied",
  approval_requested: "Physician approval requested",
  completed: "Completed",
  cancelled: "Cancelled by patient",
  info_provided: "Patient added information",
  coordination_ready: "Marked ready for Physician review",
  coordination_info_requested: "More information requested from the patient",
  coordination_closed: "Coordination task closed",
};

export function eventLabel(event) {
  if (event.event_type === "decision" || event.event_type === "decision_rejected") {
    const base = EVENT_LABELS[event.event_type];
    const reason = event.hold_reason ? ` (${event.hold_reason.replace("_", " ")})` : "";
    return `${base}: ${event.decision}${reason}`;
  }
  return EVENT_LABELS[event.event_type] ?? "Update";
}

export function actorLabel(event) {
  if (event.by_me) return "You";
  return event.actor_role ?? "System";
}

export const APPOINTMENT_CATEGORY = "appointment_scheduling";

/**
 * Splits the queue so appointment requests live only under "Scheduling Requests". With the
 * appointments feature off there is no Scheduling page, so Tasks keeps showing everything.
 * This only organizes the view; the backend still decides who may read or act on each task.
 */
export function scopeTasks(items, scope, appointmentsOn) {
  if (!appointmentsOn) return scope === "scheduling" ? [] : items;
  return items.filter((t) => (t.category === APPOINTMENT_CATEGORY) === (scope === "scheduling"));
}
