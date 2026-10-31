export const MAX_PROPOSED_TREATMENT_LENGTH = 2000;

const GUARDRAIL_DECISIONS = new Set(["Allow", "Block", "Escalate"]);
const APPROVAL_STATUSES = new Set(["Pending", "Approved", "Rejected"]);
const FINAL_APPROVAL_STATUSES = new Set(["Approved", "Rejected"]);
const STATUS_ISSUES = new Set([
  "missing_approval",
  "multiple_approvals",
  "invalid_approval_status",
  "invalid_guardrail_decision",
  "review_timestamp_mismatch",
]);

export const PERMISSION_DESCRIPTIONS = {
  view_own_record: "View your own medical record",
  submit_record_correction: "Request corrections to your medical record",
  view_own_correction_requests: "Track your correction requests",
  request_ai_assistance: "Ask the AI assistant (available in a later phase)",
};

export function validateProposedTreatment(proposedTreatment, currentTreatment) {
  const trimmed = proposedTreatment.trim();
  if (!trimmed) return "Enter the treatment you believe is correct.";
  if (proposedTreatment.length > MAX_PROPOSED_TREATMENT_LENGTH) {
    return `Keep the proposed treatment to ${MAX_PROPOSED_TREATMENT_LENGTH} characters or fewer.`;
  }
  if (typeof currentTreatment === "string" && trimmed === currentTreatment.trim()) {
    return "The proposed treatment must be different from your current treatment.";
  }
  return "";
}

function isNullableString(value) {
  return value === null || typeof value === "string";
}

export function parseRecordResponse(result) {
  if (!result || typeof result !== "object" || !Object.hasOwn(result, "record")) {
    throw new Error("The medical-record response was invalid.");
  }
  const { record } = result;
  if (record === null) return null;
  if (
    !record ||
    typeof record !== "object" ||
    Array.isArray(record) ||
    !(record.age === null || Number.isInteger(record.age)) ||
    !isNullableString(record.medical_condition) ||
    !isNullableString(record.treatment)
  ) {
    throw new Error("The medical-record response was invalid.");
  }
  return record;
}

export function parseHistoryResponse(result) {
  if (!result || typeof result !== "object" || !Array.isArray(result.items)) {
    throw new Error("The request history response was invalid.");
  }
  return result.items.map((item) => {
    if (
      !item ||
      typeof item !== "object" ||
      !Number.isInteger(item.request_id) ||
      !isNullableString(item.created_at) ||
      !isNullableString(item.reviewed_at) ||
      !(item.guardrail_decision === null || GUARDRAIL_DECISIONS.has(item.guardrail_decision)) ||
      !(item.approval_status === null || APPROVAL_STATUSES.has(item.approval_status)) ||
      !(item.status_issue === null || STATUS_ISSUES.has(item.status_issue))
    ) {
      throw new Error("The request history response was invalid.");
    }
    return item;
  });
}

export function parseSubmissionResponse(result) {
  if (
    !result ||
    typeof result !== "object" ||
    !Number.isInteger(result.request_id) ||
    result.guardrail_decision !== "Escalate" ||
    result.approval_status !== "Pending"
  ) {
    return null;
  }
  return result;
}

/** Mirrors the backend rule: anything not finally Approved/Rejected blocks. */
export function findBlockingRequest(items) {
  return (
    items.find((item) => !FINAL_APPROVAL_STATUSES.has(item.approval_status)) ?? null
  );
}

export function describeApprovalStatus(item) {
  if (item.status_issue) {
    return {
      label: "Needs attention",
      tone: "issue",
      description:
        "This request's review records are inconsistent. It will be checked by the care team; please do not resubmit.",
    };
  }
  switch (item.approval_status) {
    case "Pending":
      return {
        label: "Pending review",
        tone: "pending",
        description: "Waiting for your assigned Physician to review it.",
      };
    case "Approved":
      return {
        label: "Approved",
        tone: "approved",
        description: "Your Physician approved this correction.",
      };
    case "Rejected":
      return {
        label: "Rejected",
        tone: "rejected",
        description: "Your Physician reviewed and did not approve this correction.",
      };
    default:
      return { label: "Unknown", tone: "issue", description: "Status unavailable." };
  }
}

export function describeDecision(decision) {
  switch (decision) {
    case "Escalate":
      return { label: "Escalated for review", tone: "escalate" };
    case "Allow":
      return { label: "Allowed", tone: "allow" };
    case "Block":
      return { label: "Blocked", tone: "block" };
    default:
      return { label: "Unavailable", tone: "issue" };
  }
}

export function formatDateTime(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

/**
 * Maps a submission failure to user-facing guidance. "uncertain" outcomes
 * must never be retried automatically: the request may have been recorded.
 */
export function describeSubmissionError(error) {
  const kind = error?.kind;
  const status = error?.status;
  const detail = error instanceof Error ? error.message : "";

  if (
    kind === "timeout" ||
    kind === "network" ||
    kind === "invalid" ||
    status === 502 ||
    status === 504
  ) {
    return {
      outcome: "uncertain",
      title: "We couldn't confirm your submission",
      message:
        "Your request may or may not have been recorded. Do not submit it again yet — check your Request History first. We've refreshed it for you.",
    };
  }
  if (kind === "unreachable") {
    return {
      outcome: "not-sent",
      title: "The server is not reachable",
      message: `${detail} Nothing was submitted.`,
    };
  }
  if (status === 409) {
    return {
      outcome: "conflict",
      title: "A request is already in review",
      message: detail,
    };
  }
  if (status === 422) {
    return { outcome: "invalid", title: "Please check your proposal", message: detail };
  }
  if (status === 401 || status === 403) {
    return { outcome: "denied", title: "Not permitted", message: detail };
  }
  return {
    outcome: "not-sent",
    title: "Correction requests are unavailable",
    message: `${detail || "Please try again later."} Nothing was submitted.`,
  };
}
