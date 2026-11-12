/**
 * Pure logic for the Technician's appointment decision form. No React, no network.
 *
 * The Technician chooses a context-specific ACTION; each action maps to one internal HASF
 * guardrail decision, which is what the backend gateway evaluates and the audit trail stores:
 *
 *   Confirm Appointment        -> ALLOW
 *   Reject Request             -> BLOCK
 *   Request More Information   -> HOLD (needs_information)
 *   Refer to Physician         -> HOLD (physician_approval)
 *
 * The backend (and the database trigger) enforce every rule again, including the minimum
 * lengths below and the final availability check at confirmation.
 */
import { MAX_INTERNAL_NOTE, MAX_PATIENT_MESSAGE, availableDecisions } from "./technician.js";

export const MIN_CONFIRMATION_CHARS = 10;
export const MIN_QUESTION_CHARS = 10;
export const MIN_REJECTION_CHARS = 20;

export const APPOINTMENT_ACTIONS = {
  confirm: {
    key: "confirm",
    decision: "ALLOW",
    holdReason: null,
    title: "Confirm Appointment",
    description:
      "Book this time for the patient. We re-check the doctor's availability and reserve the slot at the same moment.",
    submit: "Confirm appointment",
    noteLabel: "Internal case note (optional)",
    messageLabel: "Confirmation message to the patient",
    messageHelp: "The patient sees this as their confirmation. Include anything they should bring or know.",
    messageRequired: true,
    noteRequired: false,
    minMessage: MIN_CONFIRMATION_CHARS,
  },
  reject: {
    key: "reject",
    decision: "BLOCK",
    holdReason: null,
    title: "Reject Request",
    description: "Decline this request. The patient is told why and can ask for another time.",
    submit: "Reject request",
    noteLabel: "Internal case note (required)",
    messageLabel: "Explanation for the patient",
    messageHelp: "Explain why this cannot be booked and what the patient can do next (at least 20 characters).",
    messageRequired: true,
    noteRequired: true,
    minMessage: MIN_REJECTION_CHARS,
  },
  request_info: {
    key: "request_info",
    decision: "HOLD",
    holdReason: "needs_information",
    title: "Request More Information",
    description: "Ask the patient a question. Review resumes when they reply.",
    submit: "Send question to patient",
    noteLabel: "Internal case note (required)",
    messageLabel: "Question for the patient",
    messageHelp: "Ask one clear question, for example what the visit is for (at least 10 characters).",
    messageRequired: true,
    noteRequired: true,
    minMessage: MIN_QUESTION_CHARS,
  },
  refer: {
    key: "refer",
    decision: "HOLD",
    holdReason: "physician_approval",
    title: "Refer to Physician",
    description: "Use only if a clinical decision is needed. The assigned physician reviews it; you cannot approve it yourself.",
    submit: "Refer to physician",
    noteLabel: "Internal case note for the physician (required)",
    messageLabel: "Message to the patient",
    messageHelp: "Tell the patient a physician is reviewing their request.",
    messageRequired: true,
    noteRequired: true,
    minMessage: 1,
  },
};

const ORDER = ["confirm", "request_info", "reject", "refer"];

/** Which of the four actions the backend would accept from the task's current status. */
export function availableAppointmentActions(task) {
  const decisions = availableDecisions(task);
  return ORDER.filter((key) => {
    const action = APPOINTMENT_ACTIONS[key];
    return decisions.includes(action.decision);
  }).map((key) => APPOINTMENT_ACTIONS[key]);
}

export const EMPTY_APPOINTMENT_FORM = Object.freeze({ action: "", internalNote: "", patientMessage: "" });

/** Pre-filled, editable wording for the confirmation message. */
export function defaultConfirmMessage(appointment) {
  if (!appointment) return "";
  const who = appointment.physician_name ?? "your doctor";
  return `Your appointment with ${who} on ${appointment.local_label} is confirmed.`;
}

export function validateAppointmentForm(form) {
  const errors = {};
  const action = APPOINTMENT_ACTIONS[form.action];
  if (!action) {
    errors.action = "Choose what you want to do with this request.";
    return { errors, valid: false };
  }
  const note = form.internalNote.trim();
  const message = form.patientMessage.trim();
  if (action.noteRequired && !note) errors.internalNote = "An internal case note is required.";
  if (note.length > MAX_INTERNAL_NOTE) errors.internalNote = `Keep this under ${MAX_INTERNAL_NOTE} characters.`;
  if (action.messageRequired && !message) {
    errors.patientMessage = "A message for the patient is required.";
  } else if (action.messageRequired && message.length < action.minMessage) {
    errors.patientMessage = `Please write at least ${action.minMessage} characters so the patient understands.`;
  }
  if (message.length > MAX_PATIENT_MESSAGE) errors.patientMessage = `Keep this under ${MAX_PATIENT_MESSAGE} characters.`;
  return { errors, valid: Object.keys(errors).length === 0 };
}

/** The exact request body: only the fields the backend accepts. */
export function buildAppointmentPayload(form) {
  const action = APPOINTMENT_ACTIONS[form.action];
  const payload = { decision: action.decision };
  if (action.holdReason) payload.hold_reason = action.holdReason;
  const note = form.internalNote.trim();
  const message = form.patientMessage.trim();
  if (note) payload.internal_note = note;
  if (message) payload.patient_message = message;
  return payload;
}

/** The internal decision a form selection is recorded as (shown for audit clarity). */
export function internalDecisionLabel(actionKey) {
  const action = APPOINTMENT_ACTIONS[actionKey];
  if (!action) return "";
  return action.holdReason ? `${action.decision} (${action.holdReason.replace("_", " ")})` : action.decision;
}

// ---- availability pre-flight -----------------------------------------------------------------

export function slotCheckView(check) {
  if (!check) return { tone: "info", blocksConfirm: false, message: "" };
  switch (check.result) {
    case "ok":
      return { tone: "success", blocksConfirm: false, message: check.message };
    case "slot_unavailable":
    case "physician_unavailable":
      return { tone: "warning", blocksConfirm: true, message: check.message };
    case "already_confirmed":
    case "not_pending":
      return { tone: "info", blocksConfirm: true, message: check.message };
    default:
      return { tone: "info", blocksConfirm: false, message: "" };
  }
}

/** Statuses in which the availability check is meaningful. */
export function shouldCheckSlot(task) {
  return Boolean(task?.appointment) && ["In Review", "Awaiting Physician"].includes(task.status);
}

// ---- results and errors ----------------------------------------------------------------------

export function describeAppointmentResult(response, actionKey) {
  const decision = APPOINTMENT_ACTIONS[actionKey]?.decision ?? response?.latest_decision ?? null;
  const base = {
    guardrail: response?.guardrail_decision ?? null,
    decision,
    recordedAs: internalDecisionLabel(actionKey),
  };
  switch (response?.outcome) {
    case "executed":
    case "recorded":
      return { ...base, tone: "success", title: successTitle(actionKey), message: response.explanation };
    case "approval_requested":
      return {
        ...base,
        tone: "info",
        title: "Referred to physician",
        message: "The assigned physician has been asked to review this request. You will see the result here.",
      };
    case "approval_reused":
      return {
        ...base,
        tone: "info",
        title: "Already referred",
        message: "A physician review is already pending for this request.",
      };
    case "escalated":
      return { ...base, tone: "warning", title: "Not applied: physician review needed", message: response.explanation };
    case "rejected":
      return { ...base, tone: "error", title: "Not applied", message: response.explanation };
    default:
      return { ...base, tone: "error", title: "Unexpected response", message: "Please refresh and check the request." };
  }
}

function successTitle(actionKey) {
  switch (actionKey) {
    case "confirm":
      return "Appointment confirmed";
    case "reject":
      return "Request rejected";
    case "request_info":
      return "Question sent";
    default:
      return "Decision recorded";
  }
}

/** `recheck` tells the form to re-run the availability check (the slot was just taken). */
export function describeAppointmentFailure(error) {
  const code = error?.code ?? null;
  if (code === "slot_unavailable") {
    return {
      recheck: true,
      message:
        "That time is no longer available, so nothing was changed. Reject the request with an explanation, or ask the patient to choose another time.",
    };
  }
  if (error?.status === 504) {
    return {
      recheck: true,
      message: `${error.message} The request has been reloaded; check its status before trying again.`,
    };
  }
  return { recheck: false, message: error?.message || "That action could not be completed. Please try again." };
}

/** Event wording for appointment requests; the internal ALLOW/BLOCK/HOLD stays visible for audit. */
export function appointmentEventLabel(event) {
  if (event.event_type === "decision") {
    if (event.decision === "ALLOW") return "Appointment confirmed (ALLOW)";
    if (event.decision === "BLOCK") return "Request rejected (BLOCK)";
    if (event.decision === "HOLD" && event.hold_reason === "needs_information") {
      return "More information requested (HOLD)";
    }
    if (event.decision === "HOLD") return "Referred to physician (HOLD)";
  }
  if (event.event_type === "decision_rejected") return "Confirmation not applied by safety check";
  if (event.event_type === "info_provided") return "Patient responded";
  return null;
}
