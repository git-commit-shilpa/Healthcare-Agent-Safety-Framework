/**
 * Pure logic for Technician handling of medical-record correction requests (Technician-first, migrations 014-015).
 *
 * Patient -> Technician -> HOLD / DENY. The Technician can:
 *   - HOLD and request Physician authorization (the database then creates the one approval, only for a Physician
 *     who can actually process it),
 *   - HOLD and ask the patient for more information,
 *   - DENY for a standard ADMINISTRATIVE reason, only before any Physician approval exists,
 *   - close the coordination task AFTER the Physician has finally approved or rejected.
 * There is deliberately NO ALLOW and no approve/reject: only the Physician authorizes a clinical change. The backend
 * and the database enforce every rule again. Nothing here (or in the task it receives) contains the correction text.
 */
import { MAX_INTERNAL_NOTE, MAX_PATIENT_MESSAGE } from "./technician.js";

export const CORRECTION_CATEGORY = "record_correction";
export const MIN_QUESTION_CHARS = 10;
export const MIN_DENY_EXPLANATION_CHARS = 20;
export const DEFAULT_AUTHORIZATION_MESSAGE = "With your Physician for review.";

export const CORRECTION_ACTIONS = {
  request_physician_authorization: {
    key: "request_physician_authorization",
    kind: "HOLD",
    title: "HOLD · Request Physician authorization",
    description: "Sends this correction to the assigned Physician. Only the Physician can approve it.",
    messageLabel: "Message to the patient (optional)",
    messageHelp: `Leave blank to send "${DEFAULT_AUTHORIZATION_MESSAGE}"`,
    noteLabel: "Internal note (required)",
    noteRequired: true,
    messageRequired: false,
    submit: "Request Physician authorization",
  },
  request_information: {
    key: "request_information",
    kind: "HOLD",
    title: "HOLD · Request more information",
    description: "Ask the patient for something that is missing. They answer on their Requests page.",
    messageLabel: "Question for the patient (required)",
    messageHelp: "Be specific. Do not ask the patient to repeat the correction itself.",
    noteLabel: "Internal note (required)",
    noteRequired: true,
    messageRequired: true,
    messageMin: MIN_QUESTION_CHARS,
    submit: "Send question to patient",
  },
  decline: {
    key: "decline",
    kind: "DENY",
    title: "DENY · Decline (administrative reason)",
    description: "Only for an administrative reason, and only before the Physician has this request.",
    messageLabel: "Explanation shown to the patient (required)",
    messageHelp: "Say plainly why it cannot be processed and what the patient can do next.",
    noteLabel: "Internal note (required)",
    noteRequired: true,
    messageRequired: true,
    messageMin: MIN_DENY_EXPLANATION_CHARS,
    needsReason: true,
    submit: "Decline request",
  },
  close: {
    key: "close",
    kind: "CLOSE",
    title: "Close coordination task",
    description: "The Physician has decided. Closing this task never changes their decision.",
    messageLabel: "Message to the patient (optional)",
    messageHelp: "The patient keeps seeing the Physician's actual outcome. Leave blank for the standard note.",
    noteLabel: "Internal note (optional)",
    noteRequired: false,
    messageRequired: false,
    submit: "Close coordination task",
  },
};

export const EMPTY_CORRECTION_FORM = { action: "", reasonCode: "", internalNote: "", patientMessage: "" };

export function isCorrectionTask(task) {
  return Boolean(task && task.category === CORRECTION_CATEGORY && task.correction);
}

/** The actions the backend says are possible right now, in a stable order. */
export function availableCorrectionActions(task) {
  const allowed = new Set(task?.correction?.allowed_actions ?? []);
  return Object.values(CORRECTION_ACTIONS).filter((action) => allowed.has(action.key));
}

export function validateCorrectionForm(form, declineReasons = []) {
  const action = CORRECTION_ACTIONS[form.action];
  const errors = {};
  if (!action) {
    errors.action = "Choose an action.";
    return { errors, valid: false };
  }
  const note = form.internalNote.trim();
  const message = form.patientMessage.trim();
  if (action.noteRequired && !note) errors.internalNote = "Add an internal note.";
  if (note.length > MAX_INTERNAL_NOTE) errors.internalNote = `Keep the note under ${MAX_INTERNAL_NOTE} characters.`;
  if (action.needsReason && !declineReasons.some((reason) => reason.code === form.reasonCode)) {
    errors.reasonCode = "Choose one of the standard administrative reasons.";
  }
  if (action.messageRequired && message.length < (action.messageMin ?? 1)) {
    errors.patientMessage = `Write at least ${action.messageMin ?? 1} characters.`;
  }
  if (message.length > MAX_PATIENT_MESSAGE) errors.patientMessage = `Keep the message under ${MAX_PATIENT_MESSAGE} characters.`;
  return { errors, valid: Object.keys(errors).length === 0 };
}

export function buildCorrectionPayload(form) {
  const note = form.internalNote.trim();
  const message = form.patientMessage.trim();
  return {
    action: form.action,
    ...(form.action === "decline" ? { reason_code: form.reasonCode } : {}),
    ...(note ? { internal_note: note } : {}),
    ...(message ? { patient_message: message } : {}),
  };
}

export function describeCorrectionResult(response) {
  const reused = response?.outcome === "approval_reused";
  return {
    tone: "success",
    title: "Saved",
    message: `${response?.message || "Saved."}${reused ? " The existing Physician approval was reused; no second one was created." : ""}`,
  };
}

/** What to tell the Technician when no action is currently available. */
export function correctionWaitingNote(task) {
  const { approval_status: approval } = task.correction;
  if (task.status === "Completed") return "This coordination task is closed. The Physician's decision is unchanged.";
  if (task.status === "Declined") return "You declined this request. The patient can see your explanation and submit a new one.";
  if (approval === "invalid") return "The linked review record could not be verified. Contact an administrator.";
  if (approval === "approved" || approval === "rejected") return "";
  if (task.status === "Submitted") return "Start the review first.";
  if (task.status === "On Hold") return "Waiting for the patient's reply.";
  return "The correction is with the Physician. Nothing else is needed from you right now.";
}
