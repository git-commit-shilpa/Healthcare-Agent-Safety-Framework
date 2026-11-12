import { useRef, useState } from "react";

import { describeError } from "../../shared/taskApi.js";
import { useRemote } from "../../shared/useRemote.js";
import { Badge, ErrorMessage, LoadingMessage } from "../../shared/workspaceUi.jsx";
import AppointmentDecisionForm from "./AppointmentDecisionForm.jsx";
import AppointmentSummary from "./AppointmentSummary.jsx";
import CorrectionCoordination from "./CorrectionCoordination.jsx";
import DecisionForm from "./DecisionForm.jsx";
import { appointmentEventLabel } from "./appointmentDecision.js";
import { isCorrectionTask } from "./correctionCoordination.js";
import { completeTask, getTask, startReview } from "./technicianApi.js";
import {
  APPROVAL_META,
  MAX_PATIENT_MESSAGE,
  actorLabel,
  canComplete,
  canStartReview,
  categoryLabel,
  eventLabel,
  formatDateTime,
  statusMeta,
} from "./technician.js";

function History({ events, appointment = false }) {
  return (
    <ol className="tc-history" aria-label="Request history">
      {events.map((event) => (
        <li key={event.event_id}>
          <div className="tc-history-head">
            <strong>{(appointment && appointmentEventLabel(event)) || eventLabel(event)}</strong>
            <span>{actorLabel(event)} · {formatDateTime(event.created_at) ?? "—"}</span>
          </div>
          {event.from_status && event.to_status && event.from_status !== event.to_status && (
            <p className="tc-history-line">{event.from_status} → {event.to_status}</p>
          )}
          {event.requester_note && (
            <p className="tc-history-line"><span className="tc-tag">Patient wrote</span> {event.requester_note}</p>
          )}
          {event.internal_note && (
            <p className="tc-note-internal">
              <span className="tc-tag tc-tag-internal">Internal note · not visible to the patient</span>
              {event.internal_note}
            </p>
          )}
          {event.patient_message && (
            <p className="tc-history-line"><span className="tc-tag">Message to patient</span> {event.patient_message}</p>
          )}
          {event.guardrail_decision && event.event_type === "decision_rejected" && (
            <p className="tc-history-line"><span className="tc-tag">Safety check</span> {event.guardrail_decision}: not applied</p>
          )}
        </li>
      ))}
    </ol>
  );
}

function CompleteForm({ taskId, token, onDone, appointment = false }) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);

  async function handleComplete() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await completeTask(token, taskId, message);
      setMessage("");
    } catch (requestError) {
      setError(describeError(requestError));
    } finally {
      await onDone();
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="ws-form">
      <h3 className="ws-section-title">{appointment ? "Complete this appointment" : "Complete this request"}</h3>
      <div className="ws-field">
        <label htmlFor={`complete-${taskId}`}>Message to the patient (optional)</label>
        <textarea
          disabled={busy}
          id={`complete-${taskId}`}
          maxLength={MAX_PATIENT_MESSAGE}
          onChange={(event) => setMessage(event.target.value)}
          rows={2}
          value={message}
        />
      </div>
      {error && <p className="ws-callout ws-callout-error" role="alert">{error}</p>}
      <div className="ws-form-actions">
        <button className="ws-btn ws-btn-primary" disabled={busy} onClick={handleComplete} type="button">
          {busy ? "Completing…" : appointment ? "Mark appointment as completed" : "Mark as completed"}
        </button>
      </div>
    </div>
  );
}

/** Shows one task, its history, and the actions the current status allows. */
export default function TaskDetailPanel({ taskId, token, onChanged }) {
  const detail = useRemote(() => getTask(token, taskId), [token, taskId]);
  const [actionError, setActionError] = useState("");
  const [lastResult, setLastResult] = useState(null);
  const [starting, setStarting] = useState(false);
  const startInFlight = useRef(false);

  async function refreshAll(result) {
    // `result` is only set by a decision; keep it visible even after the status changes.
    if (result !== undefined) setLastResult(result);
    detail.reload();
    await onChanged();
  }

  async function handleStart() {
    if (startInFlight.current) return;
    startInFlight.current = true;
    setStarting(true);
    setActionError("");
    setLastResult(null);
    try {
      await startReview(token, taskId);
    } catch (error) {
      setActionError(describeError(error));
    } finally {
      await refreshAll();
      startInFlight.current = false;
      setStarting(false);
    }
  }

  if (detail.status === "loading" && !detail.data) return <LoadingMessage>Loading request…</LoadingMessage>;
  if (detail.status === "error") return <ErrorMessage message={detail.error} onRetry={detail.reload} />;
  if (detail.status === "unavailable") {
    return <p className="ws-callout ws-callout-info" role="status">Task management is not enabled yet.</p>;
  }
  const task = detail.data;
  if (!task) return null;
  const status = statusMeta(task.status);
  const approval = APPROVAL_META[task.approval_status] ?? APPROVAL_META.none;
  const closed = ["Completed", "Declined", "Cancelled"].includes(task.status);

  return (
    <article className="tc-detail" aria-labelledby={`task-${task.task_id}-title`} aria-busy={detail.status === "loading"}>
      <header className="tc-detail-head">
        <div>
          <p className="ws-eyebrow">{categoryLabel(task.category).toUpperCase()}</p>
          <h3 id={`task-${task.task_id}-title`}>{task.title}</h3>
        </div>
        <div className="tc-badges">
          <Badge tone={status.tone}>{status.label}</Badge>
          {task.requires_physician_approval || task.approval_status !== "none" ? (
            <Badge tone={approval.tone}>{approval.label}</Badge>
          ) : null}
        </div>
      </header>

      <dl className="tc-facts">
        <div><dt>Patient</dt><dd>{task.patient.name ?? `Patient ${task.patient.patient_id}`}</dd></div>
        <div><dt>Submitted</dt><dd>{formatDateTime(task.created_at) ?? "—"}</dd></div>
        <div><dt>Last update</dt><dd>{formatDateTime(task.updated_at) ?? "—"}</dd></div>
      </dl>

      {task.appointment && <AppointmentSummary task={task} />}

      {task.description && !task.appointment && (
        <section className="tc-block" aria-label="What the patient wrote">
          <h4>What the patient wrote</h4>
          <p>{task.description}</p>
        </section>
      )}
      {task.patient_message && (
        <section className="tc-block" aria-label="Latest message to the patient">
          <h4>Latest message the patient can see</h4>
          <p>{task.patient_message}</p>
        </section>
      )}

      <section className="tc-block" aria-label="History">
        <h4>History</h4>
        <History appointment={Boolean(task.appointment)} events={task.events} />
      </section>

      {actionError && <p className="ws-callout ws-callout-error" role="alert">{actionError}</p>}

      {canStartReview(task) && (
        <div className="ws-form-actions">
          <button className="ws-btn ws-btn-primary" disabled={starting} onClick={handleStart} type="button">
            {starting ? "Starting…" : task.status === "On Hold" ? "Resume review" : "Start review"}
          </button>
        </div>
      )}
      {lastResult && (
        <div className={`ws-callout ws-callout-${lastResult.tone}`} role="status">
          <strong>{lastResult.title}</strong>
          <p>{lastResult.message}</p>
          {lastResult.guardrail && (
            <p className="tc-guardrail-line">
              Recorded internally as {lastResult.recordedAs || lastResult.decision} · safety check: {lastResult.guardrail}
            </p>
          )}
        </div>
      )}
      {isCorrectionTask(task) ? (
        <CorrectionCoordination key={task.task_id} onDone={refreshAll} task={task} token={token} />
      ) : task.appointment ? (
        <AppointmentDecisionForm key={task.task_id} onDone={refreshAll} task={task} token={token} />
      ) : (
        <DecisionForm key={task.task_id} onDone={refreshAll} task={task} token={token} />
      )}
      {canComplete(task) && (
        <CompleteForm appointment={Boolean(task.appointment)} onDone={() => refreshAll()} taskId={task.task_id} token={token} />
      )}
      {closed && <p className="ws-form-note">This request is closed. History is kept for the audit trail.</p>}
    </article>
  );
}
