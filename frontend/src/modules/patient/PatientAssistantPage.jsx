import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link, useOutletContext } from "react-router-dom";

import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import { Chip, Panel } from "../../shared/workspaceUi.jsx";
import {
  MAX_CHAT_MESSAGE_LENGTH,
  MAX_PROPOSAL_LENGTH,
  appendMessage,
  applyChatResponse,
  applyReview,
  applySaveOutcome,
  applySubmitted,
  CLASSIC_CORRECTION_FALLBACK_LINK,
  classifyChatHistoryError,
  createChatId,
  createHistoryState,
  createInitialState,
  createTurnRequest,
  describeFlowError,
  describeSaveStatus,
  formatTime,
  isFreshChat,
  parseChatListResponse,
  parseChatMessagesResponse,
  parseChatResponse,
  parseReviewResponse,
  resetFlow,
  shouldFetchChatHistory,
  shouldOfferClassicCorrectionFallback,
  validateChatMessage,
  validateProposal,
} from "./assistantChat.js";
import {
  confirmAssistantCorrection,
  patientRequest,
  reviewAssistantCorrection,
  sendAssistantMessage,
} from "./patientApi.js";
import { parseSubmissionResponse } from "./patientCorrection.js";

const now = () => new Date().toISOString();

function Bubble({ message }) {
  return (
    <li className={`chat-row chat-row-${message.role}${message.decision ? ` has-decision decision-${message.decision.toLowerCase()}` : ""}${message.emergency ? " chat-row-emergency" : ""}${message.redacted ? " chat-row-redacted" : ""}`}>
      {message.role === "assistant" && (
        <span aria-hidden="true" className="chat-message-avatar">H</span>
      )}
      <div className={`chat-bubble chat-bubble-${message.role}`}>
        <span className="ws-visually-hidden">{message.role === "user" ? "You said: " : "Assistant said: "}</span>
        {message.redacted && <span className="chat-redacted-label">Not saved · privacy</span>}
        {message.decision && (
          <span className={`chat-decision chat-decision-${message.decision.toLowerCase()}`}>
            <span aria-hidden="true" />
            {message.decision.toUpperCase()}
          </span>
        )}
        {message.emergency && (
          <p className="chat-emergency-banner" role="alert">Possible emergency: seek urgent help now.</p>
        )}
        <p>{message.text}</p>
        {message.reason && (
          <p className="chat-reason"><strong>Why:</strong> {message.reason}</p>
        )}
        {message.link && (
          <Link className="ws-link-button" to={message.link.to}>{message.link.label}</Link>
        )}
        <time className="chat-time" dateTime={message.at}>{formatTime(message.at)}</time>
      </div>
    </li>
  );
}

function AssistantChat({ session, refreshHistory }) {
  const ids = useId();
  const [chat, setChat] = useState(() => createInitialState());
  const chatRef = useRef(chat);
  const [chatId, setChatId] = useState(createChatId);
  const chatIdRef = useRef(chatId);
  const hasPersistedChat = useRef(false);
  const historyDisabled = useRef(false);
  const historyRequestInFlight = useRef(false);
  const historyMounted = useRef(false);
  const pendingTurn = useRef(null);
  const inFlight = useRef(false);
  const logRef = useRef(null);
  const detailsRef = useRef(null);
  const reviewRef = useRef(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [draftError, setDraftError] = useState("");
  const [details, setDetails] = useState("");
  const [detailsError, setDetailsError] = useState("");
  const [retryableTurn, setRetryableTurn] = useState(null);
  const [historyState, setHistoryState] = useState({ status: "loading", chats: [], error: "" });
  const [historyAction, setHistoryAction] = useState(false);
  const [saveNotice, setSaveNotice] = useState("");
  const [historyCollapsed, setHistoryCollapsed] = useState(false);
  const [latestDecision, setLatestDecision] = useState(null);

  function update(next) {
    chatRef.current = next;
    setChat(next);
  }

  const refreshChats = useCallback(async ({ quiet = false } = {}) => {
    if (!shouldFetchChatHistory(historyDisabled.current, historyRequestInFlight.current)) return;
    historyRequestInFlight.current = true;
    if (!quiet && historyMounted.current) {
      setHistoryState({ status: "loading", chats: [], error: "" });
    }
    try {
      const chats = parseChatListResponse(
        await patientRequest("/patient/assistant/chats", session?.access_token),
      );
      if (!chats) throw new Error("The server returned an invalid chat history.");
      if (historyMounted.current) setHistoryState({ status: "ready", chats, error: "" });
    } catch (error) {
      const nextState = classifyChatHistoryError(error);
      if (nextState.status === "disabled") historyDisabled.current = true;
      if (historyMounted.current) setHistoryState(nextState);
    } finally {
      historyRequestInFlight.current = false;
    }
  }, [session?.access_token]);

  useEffect(() => {
    historyMounted.current = true;
    void refreshChats();
    return () => {
      historyMounted.current = false;
    };
  }, [refreshChats]);

  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [chat.messages.length, busy]);

  useEffect(() => {
    if (chat.stage === "details") detailsRef.current?.focus();
    if (chat.stage === "review") reviewRef.current?.focus();
    if (chat.stage !== "details") setDetails("");
  }, [chat.stage]);

  /** Runs one request at a time; ignores repeated clicks while a request is in flight. */
  async function run(task) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      await task();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function showError(error, phase) {
    const outcome = describeFlowError(error, phase);
    if (shouldOfferClassicCorrectionFallback(error, phase)) {
      outcome.link = CLASSIC_CORRECTION_FALLBACK_LINK;
    }
    const current = chatRef.current;
    update(outcome.reset
      ? resetFlow(current, outcome.text, now(), outcome.link)
      : appendMessage(current, "assistant", outcome.text, now(), outcome.link));
  }

  async function executeTurn(request, appendUser) {
    if (appendUser) update(appendMessage(chatRef.current, "user", request.echo, now()));
    try {
      const result = await sendAssistantMessage(
        session?.access_token,
        request.message,
        request.stateToken,
        { chatId: request.chatId, turnId: request.turnId, isNewChat: request.isNewChat },
      );
      const parsed = parseChatResponse(result);
      if (!parsed) {
        pendingTurn.current = request;
        setRetryableTurn(request);
        update(appendMessage(
          chatRef.current,
          "assistant",
          "I got an unexpected answer. Retry this message to continue.",
          now(),
          CLASSIC_CORRECTION_FALLBACK_LINK,
        ));
        return;
      }
      update(applyChatResponse(chatRef.current, parsed, now()));
      setLatestDecision(parsed.decision);
      pendingTurn.current = null;
      setRetryableTurn(null);
      setSaveNotice(describeSaveStatus(parsed.saveStatus, parsed.saveReason));
      const outcome = applySaveOutcome(parsed.saveStatus, hasPersistedChat.current);
      hasPersistedChat.current = outcome.persisted;
      if (outcome.historyDisabled) {
        historyDisabled.current = true;
        setHistoryState({ status: "disabled", chats: [], error: "" });
      }
      if (outcome.refreshChats) void refreshChats({ quiet: true });
    } catch (error) {
      pendingTurn.current = request;
      setRetryableTurn(request);
      showError(error, "chat");
    }
  }

  /** Sends a chat turn with identifiers retained in memory for safe idempotent retry. */
  function sendChat(text, echo = text) {
    if (inFlight.current || historyAction) return;
    const request = createTurnRequest(
      chatIdRef.current,
      text,
      chatRef.current.token,
      !hasPersistedChat.current,
    );
    request.echo = echo;
    pendingTurn.current = request;
    return run(() => executeTurn(request, true));
  }

  function retryTurn() {
    const request = pendingTurn.current;
    if (!request || inFlight.current) return;
    return run(() => executeTurn(request, false));
  }

  function handleSend(event) {
    event.preventDefault();
    if (busy || historyAction || chat.stage !== "idle") return;
    const problem = validateChatMessage(draft);
    if (problem) {
      setDraftError(problem);
      return;
    }
    const text = draft.trim();
    setDraft("");
    setDraftError("");
    sendChat(text);
  }

  function handleReview(event) {
    event.preventDefault();
    if (busy || historyAction) return;
    const problem = validateProposal(details);
    if (problem) {
      setDetailsError(problem);
      return;
    }
    setDetailsError("");
    const text = details;
    run(async () => {
      try {
        const review = parseReviewResponse(
          await reviewAssistantCorrection(session?.access_token, text, chatRef.current.token),
          text,
        );
        if (!review) {
          update(resetFlow(chatRef.current, "I couldn't verify that review step. Nothing was submitted. Please start again.", now()));
          return;
        }
        update(applyReview(chatRef.current, review, now()));
      } catch (error) {
        showError(error, "review");
      }
    });
  }

  function handleConfirm() {
    const { proposal, token } = chatRef.current;
    if (busy || historyAction || chatRef.current.stage !== "review" || !proposal) return;
    run(async () => {
      try {
        const result = parseSubmissionResponse(
          await confirmAssistantCorrection(session?.access_token, proposal.text, token),
        );
        if (!result) {
          // A 2xx with unexpected content may still have been recorded.
          showError({ kind: "invalid" }, "confirm");
          return;
        }
        update(applySubmitted(chatRef.current, result, now()));
        refreshHistory?.();
      } catch (error) {
        showError(error, "confirm");
      }
    });
  }

  const flowActive = chat.stage !== "idle";

  /** Starts over. Messages and tokens live only in this component's memory, never in storage. */
  function startNewChat() {
    if (inFlight.current) return;
    const id = createChatId();
    chatIdRef.current = id;
    setChatId(id);
    hasPersistedChat.current = false;
    pendingTurn.current = null;
    setRetryableTurn(null);
    setSaveNotice("");
    setLatestDecision(null);
    update(createInitialState());
    setDraft("");
    setDraftError("");
    setDetails("");
    setDetailsError("");
  }

  function handleNewChat() {
    startNewChat();
  }

  async function selectChat(id) {
    if (inFlight.current || historyAction) return;
    setHistoryAction(true);
    setHistoryState((current) => ({ ...current, error: "" }));
    try {
      const response = await patientRequest(
        `/patient/assistant/chats/${encodeURIComponent(id)}`,
        session?.access_token,
      );
      const messages = parseChatMessagesResponse(response, id);
      if (!messages) throw new Error("The server returned an invalid conversation.");
      chatIdRef.current = id;
      setChatId(id);
      hasPersistedChat.current = true;
      pendingTurn.current = null;
      setRetryableTurn(null);
      setSaveNotice("");
      setLatestDecision(null);
      update(createHistoryState(messages));
      setDraft("");
      setDraftError("");
      setDetails("");
      setDetailsError("");
    } catch (error) {
      setHistoryState((current) => ({
        ...current,
        error: error instanceof Error ? error.message : "Could not open this conversation.",
      }));
    } finally {
      setHistoryAction(false);
    }
  }

  async function removeChat(id) {
    if (inFlight.current || historyAction) return;
    const confirmed = typeof window !== "undefined" &&
      window.confirm("Delete this conversation? This cannot be undone.");
    if (!confirmed) return;
    setHistoryAction(true);
    setHistoryState((current) => ({ ...current, error: "" }));
    try {
      await patientRequest(
        `/patient/assistant/chats/${encodeURIComponent(id)}`,
        session?.access_token,
        { method: "DELETE", allowEmptyResponse: true },
      );
      setHistoryState((current) => ({
        ...current,
        chats: current.chats.filter((item) => item.chatId !== id),
      }));
      if (chatIdRef.current === id) startNewChat();
    } catch (error) {
      setHistoryState((current) => ({
        ...current,
        error: error instanceof Error ? error.message : "Could not delete this conversation.",
      }));
    } finally {
      setHistoryAction(false);
    }
  }

  const freshChat = isFreshChat(chat);

  return (
    <section aria-labelledby={`${ids}-title`} className="chat">
      <header className="chat-header">
        <div className="chat-brand">
          <span aria-hidden="true" className="chat-avatar">H</span>
          <div>
            <p className="chat-overline">HASF <span>/</span> PATIENT AI</p>
            <h2 id={`${ids}-title`}>AI Assistant</h2>
            <p>General health information and portal guidance with safety-first responses.</p>
          </div>
        </div>
        <div className="chat-header-actions">
          <span className={`chat-header-status${latestDecision ? " has-decision" : ""}`}>
            <span aria-hidden="true" />
            {latestDecision
              ? <>Latest server decision <strong>{latestDecision.toUpperCase()}</strong></>
              : "Safety decisions appear with responses"}
          </span>
          <button
            className="chat-new"
            disabled={busy || historyAction}
            onClick={handleNewChat}
            type="button"
          >
            <span aria-hidden="true">+</span> New Chat
          </button>
        </div>
      </header>

      <div className={`chat-workspace${historyCollapsed ? " is-history-collapsed" : ""}`}>
        <aside aria-labelledby={`${ids}-history-title`} className="chat-history">
          <div className="chat-history-heading">
            <div>
              <p className="chat-overline">YOUR WORKSPACE</p>
              <h3 id={`${ids}-history-title`}>Conversations</h3>
            </div>
            <button
              aria-controls={`${ids}-history-content`}
              aria-expanded={!historyCollapsed}
              aria-label={historyCollapsed ? "Expand chat history" : "Collapse chat history"}
              className="chat-history-toggle"
              onClick={() => setHistoryCollapsed((collapsed) => !collapsed)}
              type="button"
            >
              <span aria-hidden="true">{historyCollapsed ? "›" : "‹"}</span>
            </button>
          </div>
            <div className="chat-history-content" hidden={historyCollapsed} id={`${ids}-history-content`}>
              {historyState.status === "loading" && <p className="chat-history-note">Loading conversations…</p>}
              {historyState.status === "disabled" && (
                <p className="chat-history-note">Chat history is off. Your assistant is still ready.</p>
              )}
              {historyState.status === "ready" && historyState.chats.length === 0 && (
                <p className="chat-history-note">No saved conversations yet.</p>
              )}
              {historyState.status === "error" && (
                <div className="chat-history-error">
                  <p role="alert">{historyState.error}</p>
                  <button disabled={historyAction} onClick={() => refreshChats()} type="button">
                    Retry history
                  </button>
                </div>
              )}
              {historyState.error && historyState.status !== "error" && (
                <p className="chat-history-error" role="alert">{historyState.error}</p>
              )}
              {historyState.status === "ready" && historyState.chats.length > 0 && (
                <ul aria-label="Saved conversations" className="chat-history-list">
                  {historyState.chats.map((item) => (
                    <li className="chat-history-item" key={item.chatId}>
                      <button
                        aria-current={chatId === item.chatId ? "true" : undefined}
                        className="chat-history-open"
                        disabled={busy || historyAction}
                        onClick={() => selectChat(item.chatId)}
                        type="button"
                      >
                        <span className="chat-history-mark" aria-hidden="true" />
                        <span className="chat-history-copy">
                          <span>{item.title || "Conversation"}</span>
                          <small>{formatTime(item.lastActivityAt)} · {item.messageCount} messages</small>
                        </span>
                      </button>
                      <button
                        aria-label={`Delete ${item.title || "conversation"}`}
                        className="chat-history-delete"
                        disabled={busy || historyAction}
                        onClick={() => removeChat(item.chatId)}
                        type="button"
                      >
                        Delete
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <p className="chat-history-footnote">
                {historyState.status === "disabled"
                  ? "History is unavailable while persistence is disabled."
                  : "Conversations are private to your account."}
              </p>
            </div>
        </aside>

        <main className="chat-main">
          <div className="chat-session-bar">
            <div>
              <span className="chat-session-kicker">SECURE SESSION</span>
              <h3>{freshChat ? "A new conversation" : "Conversation"}</h3>
            </div>
            <span className="chat-session-mark"><span /> Patient support</span>
          </div>

          {freshChat ? (
            <div aria-label="Conversation" aria-live="polite" className="chat-empty-state" role="log">
              <div aria-hidden="true" className="chat-empty-orbit">
                <span className="chat-empty-core">H</span>
                <i className="chat-orbit-one" />
                <i className="chat-orbit-two" />
              </div>
              <p className="chat-overline">A CLEARER WAY TO NAVIGATE YOUR PORTAL</p>
              <h3>How can I help you today?</h3>
              <p className="chat-empty-copy">
                Hi! I can explain medical terms and general health topics, how the portal works,
                and how correction requests are reviewed. I can't diagnose, prescribe, or change your record.
              </p>
              <div aria-label="Example prompts" className="chat-prompt-grid" role="group">
                <button
                  className="chat-prompt"
                  disabled={busy || historyAction}
                  onClick={() => sendChat("How does a correction request get reviewed?")}
                  type="button"
                >
                  <span className="chat-prompt-index">01</span>
                  <span><strong>Understand the process</strong><small>How are corrections reviewed?</small></span>
                  <span aria-hidden="true" className="chat-prompt-arrow">↗</span>
                </button>
                <button
                  className="chat-prompt"
                  disabled={busy || historyAction}
                  onClick={() => sendChat("Where can I view my medical record?")}
                  type="button"
                >
                  <span className="chat-prompt-index">02</span>
                  <span><strong>Find your record</strong><small>Where can I view my record?</small></span>
                  <span aria-hidden="true" className="chat-prompt-arrow">↗</span>
                </button>
                <button
                  className="chat-prompt chat-prompt-featured"
                  disabled={busy || historyAction}
                  onClick={() => sendChat("I want to request a correction to my record.")}
                  type="button"
                >
                  <span className="chat-prompt-index">03</span>
                  <span><strong>Request a correction</strong><small>Start a physician-reviewed request</small></span>
                  <span aria-hidden="true" className="chat-prompt-arrow">↗</span>
                </button>
              </div>
            </div>
          ) : (
            <ol aria-label="Conversation" aria-live="polite" aria-relevant="additions" className="chat-log" ref={logRef} role="log">
              {chat.messages.map((message) => <Bubble key={message.id} message={message} />)}
              {busy && (
                <li className="chat-row chat-row-assistant">
                  <span aria-hidden="true" className="chat-message-avatar">H</span>
                  <div className="chat-bubble chat-bubble-assistant chat-typing">
                    <span aria-hidden="true" className="chat-dots"><i /><i /><i /></span>
                    <span className="ws-visually-hidden">The assistant is responding…</span>
                  </div>
                </li>
              )}
            </ol>
          )}

          <div className="chat-compose-area">
            {saveNotice && <p className="chat-save-status" role="status">{saveNotice}</p>}
            {retryableTurn && (
              <div className="chat-retry-row">
                <p>Your last message hasn't received a confirmed response.</p>
                <button disabled={busy || historyAction} onClick={retryTurn} type="button">
                  {busy ? "Retrying…" : "Retry last message"}
                </button>
              </div>
            )}

      {chat.stage === "offer" && (
        <div aria-label="Choose an option" className="chat-actions" role="group">
          <button className="ws-btn ws-btn-primary" disabled={busy || historyAction} onClick={() => sendChat("yes", "Yes, please")} type="button">
            Yes, help me raise a request
          </button>
          <button className="ws-btn ws-btn-secondary" disabled={busy || historyAction} onClick={() => sendChat("no", "No, thanks")} type="button">
            No, thanks
          </button>
        </div>
      )}

      {chat.stage === "details" && (
        <form className="chat-card ws-form" noValidate onSubmit={handleReview}>
          <div className="ws-field">
            <label htmlFor={`${ids}-details`}>Proposed correction (sent securely, never to the AI)</label>
            <textarea
              aria-describedby={`${ids}-details-count${detailsError ? ` ${ids}-details-err` : ""}`}
              aria-invalid={detailsError ? "true" : "false"}
              disabled={busy || historyAction}
              id={`${ids}-details`}
              maxLength={MAX_PROPOSAL_LENGTH}
              onChange={(event) => {
                setDetails(event.target.value);
                if (detailsError) setDetailsError("");
              }}
              ref={detailsRef}
              rows={4}
              value={details}
            />
            <div className="ws-field-meta">
              <span id={`${ids}-details-count`}>{details.length} / {MAX_PROPOSAL_LENGTH}</span>
            </div>
            {detailsError && <p className="ws-field-error" id={`${ids}-details-err`} role="alert">{detailsError}</p>}
          </div>
          <div className="ws-form-actions">
            <button className="ws-btn ws-btn-primary" disabled={busy || historyAction} type="submit">Review request</button>
            <button className="ws-btn ws-btn-secondary" disabled={busy || historyAction} onClick={() => sendChat("cancel", "Cancel")} type="button">
              Cancel
            </button>
          </div>
        </form>
      )}

      {chat.stage === "review" && chat.proposal && (
        <div aria-labelledby={`${ids}-review`} className="chat-card" ref={reviewRef} role="group" tabIndex={-1}>
          <h3 id={`${ids}-review`}>Review your correction request</h3>
          <p className="chat-proposal">{chat.proposal.text}</p>
          <p className="ws-form-note">
            {chat.proposal.notice} This step expires in {Math.round(chat.proposal.expiresIn / 60)} minutes.
          </p>
          <div className="ws-form-actions">
            <button className="ws-btn ws-btn-primary" disabled={busy || historyAction} onClick={handleConfirm} type="button">
              {busy ? "Submitting…" : "Confirm and submit"}
            </button>
            <button className="ws-btn ws-btn-secondary" disabled={busy || historyAction} onClick={() => sendChat("cancel", "Cancel")} type="button">
              Cancel
            </button>
          </div>
        </div>
      )}

      <form aria-busy={busy} className="chat-composer" noValidate onSubmit={handleSend}>
        <label className="ws-visually-hidden" htmlFor={`${ids}-msg`}>Message the assistant</label>
        <textarea
          aria-describedby={draftError ? `${ids}-msg-err` : undefined}
          aria-invalid={draftError ? "true" : "false"}
          disabled={busy || historyAction || flowActive}
          id={`${ids}-msg`}
          maxLength={MAX_CHAT_MESSAGE_LENGTH}
          onChange={(event) => {
            setDraft(event.target.value);
            if (draftError) setDraftError("");
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) handleSend(event);
          }}
          placeholder={flowActive ? "Use the options above to continue" : "Ask a health question or about the portal…"}
          rows={1}
          value={draft}
        />
        <button className="chat-send" disabled={busy || historyAction || flowActive} type="submit">
          <span>{busy ? "Sending…" : "Send"}</span>
          <span aria-hidden="true" className="chat-send-icon">{busy ? "…" : "↑"}</span>
        </button>
        {draftError && <p className="ws-field-error chat-composer-error" id={`${ids}-msg-err`} role="alert">{draftError}</p>}
      </form>
      <p className="chat-compose-hint">Enter to send <span>·</span> Shift + Enter for a new line <span>·</span> General information only, not medical advice</p>
          </div>
        </main>
      </div>
    </section>
  );
}

/** Conversational Patient assistant. Corrections only go through the review and Confirm steps. */
export default function PatientAssistantPage() {
  const { session, refreshHistory } = useOutletContext() ?? {};
  return (
    <>
      <PageHeader
        description="Ask general health questions, how the portal works, or how corrections are reviewed."
        eyebrow="PATIENT SUPPORT"
        icon="assistant"
        title="Ask AI Assistant"
      />
      <Panel
        aside={<Chip>Educational only</Chip>}
        className="assistant-command-panel"
        eyebrow="GUARDED ASSISTANT"
        icon="assistant"
        title="Chat"
        titleId="assistant-title"
        tone={3}
      >
        {/* A different login session starts a fresh conversation. */}
        <AssistantChat
          key={session?.user?.id ?? "none"}
          refreshHistory={refreshHistory}
          session={session}
        />
      </Panel>
    </>
  );
}
