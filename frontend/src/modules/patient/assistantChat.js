// Pure conversation logic for the Patient chat assistant (no React, no network).
// The server owns all state tokens; this module only stores and echoes them.

export const MAX_CHAT_MESSAGE_LENGTH = 500;
export const MAX_PROPOSAL_LENGTH = 2000;

const CHAT_MODES = new Set([
  "answer", "refusal", "notice", "pointer", "greeting", "offer", "details_step", "cancelled", "record",
]);
const MAX_REPLY_LENGTH = 2000;
const MAX_TOKEN_LENGTH = 600;
const CHAT_SAVE_STATUSES = new Set(["saved", "duplicate", "not_saved", "unconfirmed", "disabled"]);
const CHAT_SAVE_REASONS = new Set([
  "no_ids", "filtered", "not_eligible", "sensitive", "refused", "redacted",
  "chat_unavailable", "chat_full", "chat_limit", "error",
]);
// A correction-flow step is stored only as these fixed placeholders (never the Patient's words).
const REDACTED_USER_PLACEHOLDER = "Correction request step";
const REDACTED_NOTICE =
  "Correction-request steps in this conversation are not shown. For your privacy, what you typed and the details of those steps were never saved. Your correction requests and their review status are in AI Request History.";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const HISTORY_LINK = { to: "/dashboard/patient/history", label: "Open AI Request History" };
export const RECORDS_LINK = { to: "/dashboard/patient/records", label: "Open My Medical Records" };
export const CLASSIC_CORRECTION_FALLBACK_LINK = {
  to: "/dashboard/patient/records?correctionFallback=1",
  label: "Use the secure correction form",
};

export function shouldOfferClassicCorrectionFallback(error, phase) {
  if (phase === "confirm") return false;
  return (
    ["timeout", "network", "unreachable", "invalid"].includes(error?.kind) ||
    (Number.isInteger(error?.status) && error.status >= 500)
  );
}

const WELCOME =
  "Hi! I can explain medical terms and general health topics, how the portal works, and how correction requests are reviewed. I can't diagnose, prescribe, or change your record.";

export function createInitialState(at = new Date().toISOString()) {
  return {
    messages: [{ id: 1, role: "assistant", text: WELCOME, at }],
    stage: "idle", // idle | offer | details | review
    token: null, // the latest server-issued token, stored exactly as received
    proposal: null, // { text, notice, expiresIn } while reviewing
  };
}

export function createChatId() {
  if (typeof globalThis.crypto?.randomUUID !== "function") {
    throw new Error("Secure random UUID generation is unavailable.");
  }
  return globalThis.crypto.randomUUID();
}

export function createTurnRequest(chatId, message, stateToken, isNewChat, turnId = createChatId()) {
  return { chatId, turnId, message, stateToken, isNewChat };
}

export function parseChatListResponse(result) {
  if (!result || typeof result !== "object" || !Array.isArray(result.chats)) return null;
  const chats = [];
  for (const chat of result.chats) {
    if (
      !chat || typeof chat !== "object" || !UUID_PATTERN.test(chat.chat_id) ||
      typeof chat.title !== "string" || typeof chat.last_activity_at !== "string" ||
      !Number.isInteger(chat.message_count) || chat.message_count < 0
    ) return null;
    chats.push({
      chatId: chat.chat_id,
      title: chat.title,
      lastActivityAt: chat.last_activity_at,
      messageCount: chat.message_count,
    });
  }
  return chats;
}

export function parseChatMessagesResponse(result, expectedChatId) {
  if (
    !result || typeof result !== "object" || result.chat_id !== expectedChatId ||
    !Array.isArray(result.messages)
  ) return null;
  const messages = [];
  for (const message of result.messages) {
    if (
      !message || typeof message !== "object" ||
      !["user", "assistant"].includes(message.role) ||
      typeof message.content !== "string" || typeof message.created_at !== "string"
    ) return null;
    const redacted = message.role === "assistant" && message.display_mode === "event";
    messages.push({
      role: message.role,
      // Older servers send the stored placeholder text; show the privacy notice instead.
      text: redacted ? REDACTED_NOTICE : message.content,
      at: message.created_at,
      ...(redacted ? { redacted: true } : {}),
    });
  }
  return collapseRedactedSteps(messages);
}

/**
 * A reopened conversation never shows stored correction placeholders as if they were chat.
 * Each run of "placeholder user row + redacted assistant row" becomes ONE redacted notice.
 * A user row is dropped only when it is exactly the placeholder AND directly followed by a
 * redacted row, so the Patient's own messages are never hidden. The server already does this;
 * repeating it here keeps older servers and cached responses from showing fake messages.
 */
export function collapseRedactedSteps(messages) {
  const out = [];
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    const next = messages[index + 1];
    const isPlaceholderPair =
      message.role === "user" && !message.redacted && message.text === REDACTED_USER_PLACEHOLDER &&
      next?.role === "assistant" && next.redacted === true;
    if (isPlaceholderPair || message.redacted === true) {
      const last = out[out.length - 1];
      if (!last?.redacted) {
        const notice = isPlaceholderPair ? next : message;
        out.push({ role: "assistant", text: REDACTED_NOTICE, at: notice.at, redacted: true, link: HISTORY_LINK });
      }
      if (isPlaceholderPair) index += 1;
      continue;
    }
    out.push(message);
  }
  return out;
}

/** True only for the empty welcome screen. A reopened chat holding just a privacy notice is a real conversation. */
export function isFreshChat(state) {
  return state.messages.length === 1 && state.stage === "idle" && !state.messages[0].redacted;
}

export function createHistoryState(messages) {
  return {
    messages: messages.map((message, index) => ({ ...message, id: index + 1 })),
    stage: "idle",
    token: null,
    proposal: null,
  };
}

export function describeSaveStatus(status, reason = null) {
  if ((status === "saved" || status === "duplicate") && reason === "redacted") {
    return "Correction-request steps aren't saved word for word. Only a privacy placeholder was kept in chat history.";
  }
  if (status === "saved" || status === "duplicate") return "This conversation was saved to chat history.";
  if (status === "not_saved") {
    if (reason === "filtered") return "This message wasn't saved to chat history to protect your privacy.";
    if (reason === "sensitive") return "Replies that show your medical record aren't saved to chat history, to protect your privacy.";
    if (reason === "refused") return "Refused or blocked exchanges aren't saved to chat history.";
    if (reason === "not_eligible") return "This exchange isn't kept in chat history.";
    // Unknown or older reason codes fall back to the neutral wording.
    return "This message wasn't saved to chat history.";
  }
  if (status === "unconfirmed") {
    return "We couldn't confirm whether this conversation was saved. It may not appear in chat history.";
  }
  if (status === "disabled") return "Chat history is turned off. Your conversation is not being saved.";
  return "";
}

/**
 * What a chat response's save_status means for the conversation.
 * Only saved/duplicate prove the chat exists in history. disabled, not_saved, unconfirmed
 * and a missing status leave `persisted` unchanged, so the next turn still says is_new_chat
 * until a save really succeeds (and a later-enabled backend can still create the chat).
 * historyDisabled: true = show the disabled state, false = history works, null = no information.
 */
export function applySaveOutcome(saveStatus, persisted) {
  if (saveStatus === "saved" || saveStatus === "duplicate") {
    return { persisted: true, refreshChats: true, historyDisabled: false };
  }
  if (saveStatus === "disabled") return { persisted, refreshChats: false, historyDisabled: true };
  return { persisted, refreshChats: false, historyDisabled: null };
}

export function shouldFetchChatHistory(historyDisabled, requestInFlight) {
  return !historyDisabled && !requestInFlight;
}

export function classifyChatHistoryError(error) {
  if (error?.status === 404) return { status: "disabled", chats: [], error: "" };
  return {
    status: "error",
    chats: [],
    error: error instanceof Error ? error.message : "Could not load chat history.",
  };
}

export function appendMessage(state, role, text, at, link = null) {
  const id = state.messages.length ? state.messages[state.messages.length - 1].id + 1 : 1;
  return { ...state, messages: [...state.messages, { id, role, text, at, ...(link ? { link } : {}) }] };
}

function isToken(value) {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_TOKEN_LENGTH;
}

/** Returns { reply, mode, token } or null when the response is not trustworthy. */
export function parseChatResponse(result) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  const { reply, mode, state_token: token } = result;
  if (typeof reply !== "string" || !reply.trim() || reply.length > MAX_REPLY_LENGTH) return null;
  if (!CHAT_MODES.has(mode)) return null;
  if (token !== null && token !== undefined && !isToken(token)) return null;
  // Offers and detail steps are only usable with a server token.
  if ((mode === "offer" || mode === "details_step") && !isToken(token)) return null;
  return {
    reply,
    mode,
    token: token ?? null,
    decision: ["Allow", "Block", "Escalate"].includes(result.decision) ? result.decision : null,
    intent: typeof result.intent === "string" ? result.intent.slice(0, 60) : null,
    reason: typeof result.reason === "string" && result.reason.trim() ? result.reason.slice(0, 400) : null,
    saveStatus: CHAT_SAVE_STATUSES.has(result.save_status) ? result.save_status : null,
    saveReason: CHAT_SAVE_REASONS.has(result.save_reason) ? result.save_reason : null,
  };
}

/** The reviewed text must be exactly what was sent. */
export function parseReviewResponse(result, sentText) {
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  if (result.proposed_treatment !== sentText || !isToken(result.state_token)) return null;
  if (typeof result.notice !== "string" || !Number.isInteger(result.expires_in_seconds)) return null;
  return { text: result.proposed_treatment, token: result.state_token, notice: result.notice, expiresIn: result.expires_in_seconds };
}

export function applyChatResponse(state, parsed, at) {
  const next = appendMessage(state, "assistant", parsed.reply, at);
  const lastMessage = next.messages.length - 1;
  if (parsed.decision) {
    next.messages[lastMessage] = {
      ...next.messages[lastMessage],
      decision: parsed.decision,
      ...(parsed.reason ? { reason: parsed.reason } : {}),
      ...(parsed.intent === "emergency" ? { emergency: true } : {}),
    };
  }
  if (parsed.mode === "offer") return { ...next, stage: "offer", token: parsed.token, proposal: null };
  if (parsed.mode === "details_step") return { ...next, stage: "details", token: parsed.token, proposal: null };
  const link =
    parsed.mode === "pointer"
      ? /history/i.test(parsed.reply) ? HISTORY_LINK : RECORDS_LINK
      : null;
  const withLink = link ? { ...next, messages: next.messages.map((m, i) => (i === next.messages.length - 1 ? { ...m, link } : m)) } : next;
  return { ...withLink, stage: "idle", token: null, proposal: null };
}

export function applyReview(state, review, at) {
  const next = appendMessage(state, "assistant", "Please review your request below. Nothing has been submitted yet.", at);
  return { ...next, stage: "review", token: review.token, proposal: { text: review.text, notice: review.notice, expiresIn: review.expiresIn } };
}

export function applySubmitted(state, result, at) {
  const next = appendMessage(
    state,
    "assistant",
    `Your correction request #${result.request_id} was submitted. It is Pending review by your assigned Physician, and your record has not changed.`,
    at,
    HISTORY_LINK,
  );
  return { ...next, stage: "idle", token: null, proposal: null };
}

export function resetFlow(state, text, at, link = null) {
  return { ...appendMessage(state, "assistant", text, at, link), stage: "idle", token: null, proposal: null };
}

export function validateProposal(text) {
  if (!text.trim()) return "Enter the correction you'd like to request.";
  if (text.length > MAX_PROPOSAL_LENGTH) return `Keep it under ${MAX_PROPOSAL_LENGTH} characters.`;
  return "";
}

export function validateChatMessage(text) {
  if (!text.trim()) return "Type a message first.";
  if (text.length > MAX_CHAT_MESSAGE_LENGTH) return `Keep your message under ${MAX_CHAT_MESSAGE_LENGTH} characters.`;
  return "";
}

/**
 * Maps a failed request to what the Patient sees and whether the correction flow must restart.
 * phase: "chat" | "review" | "confirm". Submissions are never retried automatically.
 */
export function describeFlowError(error, phase) {
  const kind = error?.kind;
  const status = error?.status;
  const detail = error instanceof Error ? error.message : "";
  const noResponse = kind === "timeout" || kind === "network" || kind === "unreachable" || kind === "invalid";

  if (phase === "confirm" && (noResponse || status === 502 || status === 504)) {
    return {
      text: "I couldn't confirm whether your request was submitted. Please don't submit it again yet. Check AI Request History first. I won't retry automatically.",
      reset: true,
      link: HISTORY_LINK,
    };
  }
  if (status === 409 && /awaiting Physician review/i.test(detail)) {
    return { text: `${detail} You can follow it in AI Request History.`, reset: true, link: HISTORY_LINK };
  }
  if (status === 409 && /no longer valid/i.test(detail)) {
    return { text: "That step has expired or was already used. Nothing was submitted. Start again whenever you're ready.", reset: true, link: null };
  }
  if (status === 409 && /sign in again/i.test(detail)) {
    return { text: "Your session changed. Please sign in again to continue. Nothing was submitted.", reset: true, link: null };
  }
  if (status === 409) {
    return { text: `${detail || "That couldn't be completed."} Nothing was submitted.`, reset: true, link: HISTORY_LINK };
  }
  if (status === 401) return { text: "Your session has expired. Please sign in again.", reset: true, link: null };
  if (status === 403) return { text: "Your account doesn't have access to this.", reset: true, link: null };
  if (noResponse || status === 502 || status === 504) {
    return {
      text:
        phase === "chat"
          ? "I couldn't reach the assistant in time. Nothing was changed. Please try again."
          : "I couldn't confirm that step. Nothing was submitted. Please start the request again if you still want to raise it.",
      reset: phase !== "chat",
      link: null,
    };
  }
  if (status === 422) {
    return {
      text: phase === "chat" ? "That message couldn't be processed. Please shorten or rephrase it." : "Please check the correction text (1 to 2,000 characters).",
      reset: phase === "confirm",
      link: null,
    };
  }
  if (phase === "confirm") {
    return { text: "Your request couldn't be submitted right now. Please check AI Request History before trying again.", reset: true, link: HISTORY_LINK };
  }
  return {
    text: phase === "chat" ? "The assistant is unavailable right now. Please try again shortly." : "That step is unavailable right now. Please start again shortly.",
    reset: phase !== "chat",
    link: null,
  };
}

export function formatTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
}
