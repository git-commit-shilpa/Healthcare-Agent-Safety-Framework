/**
 * Pure logic for the Patient "Service requests" page. No React, no network.
 * The backend validates and authorizes everything again; this only shapes the form.
 */

export const MAX_TITLE = 200;
export const MAX_DESCRIPTION = 1000;
export const MAX_INFO_MESSAGE = 1000;

// What the patient sees for each internal status. Internal reasoning is never shown.
export const PATIENT_STATUS = {
  Submitted: { label: "Received", tone: "pending", help: "Your care team has your request." },
  "In Review": { label: "Being reviewed", tone: "pending", help: "A member of your care team is reviewing it." },
  "On Hold": { label: "Needs your reply", tone: "escalate", help: "Your care team needs more information from you." },
  "Awaiting Physician": { label: "With your physician", tone: "escalate", help: "A physician is reviewing this request." },
  "In Progress": { label: "In progress", tone: "allow", help: "Your request has been accepted and is being handled." },
  Completed: { label: "Completed", tone: "approved", help: "This request is finished." },
  Declined: { label: "Not accepted", tone: "block", help: "Your care team could not process this request." },
  Cancelled: { label: "Cancelled", tone: "inactive", help: "You cancelled this request." },
};

export function patientStatus(status) {
  return PATIENT_STATUS[status] ?? { label: String(status ?? "Unknown"), tone: "neutral", help: "" };
}

export const EMPTY_REQUEST_FORM = Object.freeze({ category: "", title: "", description: "" });

export function validateRequestForm(form, categories = []) {
  const errors = {};
  const title = form.title.trim();
  const description = form.description.trim();
  if (!form.category) errors.category = "Choose what you need help with.";
  else if (categories.length > 0 && !categories.some((c) => c.value === form.category)) {
    errors.category = "Choose a category from the list.";
  }
  if (!title) errors.title = "Give your request a short title.";
  else if (title.length > MAX_TITLE) errors.title = `Keep the title under ${MAX_TITLE} characters.`;
  if (description.length > MAX_DESCRIPTION) errors.description = `Keep this under ${MAX_DESCRIPTION} characters.`;
  return { errors, valid: Object.keys(errors).length === 0 };
}

/** Only the fields the backend accepts. The requester and status are never sent. */
export function buildCreatePayload(form) {
  const payload = { category: form.category, title: form.title.trim() };
  const description = form.description.trim();
  if (description) payload.description = description;
  return payload;
}

export function validateInfoMessage(message) {
  const text = message.trim();
  if (!text) return "Write your reply first.";
  if (text.length > MAX_INFO_MESSAGE) return `Keep this under ${MAX_INFO_MESSAGE} characters.`;
  return null;
}

/** Splits requests so the ones needing the patient's reply come first. */
export function sortRequests(items) {
  const rank = (item) => (item.can_add_info ? 0 : item.can_cancel ? 1 : 2);
  return [...items].sort((a, b) => rank(a) - rank(b));
}

export function needsReplyCount(items) {
  return items.filter((item) => item.can_add_info).length;
}

/**
 * Maps a failed submission to what the patient should see. An emergency is always shown
 * as an urgent alert; nothing about the request is stored in that case.
 */
export function describeSubmitFailure(error) {
  const code = error?.code ?? null;
  const message = error?.message || "Your request could not be submitted.";
  if (code === "emergency_detected") return { tone: "error", urgent: true, message };
  if (code === "duplicate_request") return { tone: "warning", urgent: false, message };
  if (code === "no_eligible_technician") return { tone: "warning", urgent: false, message };
  if (error?.status === 504) return { tone: "warning", urgent: false, message };
  return { tone: "error", urgent: false, message };
}

export function formatDateTime(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
