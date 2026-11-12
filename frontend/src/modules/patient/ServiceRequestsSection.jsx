import { useId, useRef, useState } from "react";
import { useOutletContext } from "react-router-dom";

import { readFeatureFlags } from "../../app/features.js";

import { describeError } from "../../shared/taskApi.js";
import { useRemote } from "../../shared/useRemote.js";
import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import {
  Badge,
  EmptyMessage,
  ErrorMessage,
  LoadingMessage,
  Panel,
  RefreshButton,
} from "../../shared/workspaceUi.jsx";
import {
  EMPTY_REQUEST_FORM,
  MAX_DESCRIPTION,
  MAX_INFO_MESSAGE,
  MAX_TITLE,
  buildCreatePayload,
  describeSubmitFailure,
  formatDateTime,
  needsReplyCount,
  patientStatus,
  sortRequests,
  validateInfoMessage,
  validateRequestForm,
} from "./serviceRequests.js";
import {
  addRequestInfo,
  cancelRequest,
  listCategories,
  listRequests,
  submitRequest,
} from "./serviceRequestsApi.js";

function NewRequestForm({ categories, token, onSubmitted }) {
  const baseId = useId();
  const [form, setForm] = useState({ ...EMPTY_REQUEST_FORM });
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const inFlight = useRef(false);
  const { errors, valid } = validateRequestForm(form, categories);
  const selected = categories.find((c) => c.value === form.category);

  function update(patch) {
    setForm((current) => ({ ...current, ...patch }));
    setNotice(null);
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current) return;
    setTouched(true);
    if (!valid) return;
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const response = await submitRequest(token, buildCreatePayload(form));
      setForm({ ...EMPTY_REQUEST_FORM });
      setTouched(false);
      setNotice({
        tone: "success",
        message: response.requires_physician_review
          ? "Your request was sent. A physician will review it, and your care team will update you here."
          : "Your request was sent. Your care team will update you here.",
      });
      await onSubmitted();
    } catch (error) {
      const failure = describeSubmitFailure(error);
      setNotice({ tone: failure.tone, urgent: failure.urgent, message: failure.message });
      // A 504 means we do not know whether it was saved: show the list so the patient can check.
      if (error?.status === 504) await onSubmitted();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <form className="ws-form" noValidate onSubmit={handleSubmit} aria-busy={busy}>
      <div className="ws-field">
        <label htmlFor={`${baseId}-category`}>What do you need help with?</label>
        <select
          aria-describedby={touched && errors.category ? `${baseId}-category-error` : undefined}
          aria-invalid={touched && Boolean(errors.category)}
          disabled={busy}
          id={`${baseId}-category`}
          onChange={(event) => update({ category: event.target.value })}
          value={form.category}
        >
          <option value="">Choose a category…</option>
          {categories.map((category) => (
            <option key={category.value} value={category.value}>
              {category.label}{category.requires_physician_review ? " (physician reviews)" : ""}
            </option>
          ))}
        </select>
        {touched && errors.category && (
          <p className="ws-field-error" id={`${baseId}-category-error`} role="alert">{errors.category}</p>
        )}
        {selected?.requires_physician_review && (
          <p className="ws-form-note">
            Medication, treatment and diagnosis requests are always reviewed by a physician. Your care team cannot approve them on their own.
          </p>
        )}
      </div>

      <div className="ws-field">
        <label htmlFor={`${baseId}-title`}>Short title</label>
        <input
          aria-describedby={touched && errors.title ? `${baseId}-title-error` : undefined}
          aria-invalid={touched && Boolean(errors.title)}
          disabled={busy}
          id={`${baseId}-title`}
          maxLength={MAX_TITLE}
          onChange={(event) => update({ title: event.target.value })}
          type="text"
          value={form.title}
        />
        {touched && errors.title && (
          <p className="ws-field-error" id={`${baseId}-title-error`} role="alert">{errors.title}</p>
        )}
      </div>

      <div className="ws-field">
        <label htmlFor={`${baseId}-description`}>Details (optional)</label>
        <p className="ws-form-note" id={`${baseId}-description-help`}>
          This is not for emergencies. If you may be in danger, call your local emergency number now.
        </p>
        <textarea
          aria-describedby={`${baseId}-description-help`}
          disabled={busy}
          id={`${baseId}-description`}
          maxLength={MAX_DESCRIPTION}
          onChange={(event) => update({ description: event.target.value })}
          rows={5}
          value={form.description}
        />
        <div className="ws-field-meta"><span>{form.description.length}/{MAX_DESCRIPTION}</span></div>
        {touched && errors.description && <p className="ws-field-error" role="alert">{errors.description}</p>}
      </div>

      {notice && (
        <p
          className={`ws-callout ws-callout-${notice.tone === "success" ? "success" : notice.tone}`}
          role={notice.urgent || notice.tone === "error" ? "alert" : "status"}
        >
          {notice.urgent && <strong>Urgent: </strong>}
          {notice.message}
        </p>
      )}

      <div className="ws-form-actions">
        <button className="ws-btn ws-btn-primary" disabled={busy} type="submit">
          {busy ? "Sending…" : "Send request"}
        </button>
      </div>
    </form>
  );
}

function RequestCard({ request, token, onChanged }) {
  const status = patientStatus(request.status);
  const [mode, setMode] = useState(null); // null | "cancel" | "reply"
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const replyId = useId();

  async function run(action) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await action();
      setMode(null);
      setReply("");
    } catch (requestError) {
      setError(describeError(requestError));
    } finally {
      await onChanged();
      inFlight.current = false;
      setBusy(false);
    }
  }

  function handleReply() {
    const problem = validateInfoMessage(reply);
    if (problem) {
      setError(problem);
      return;
    }
    run(() => addRequestInfo(token, request.task_id, reply));
  }

  return (
    <li className="sr-card">
      <div className="sr-card-top">
        <div>
          <p className="ws-eyebrow">{request.category_label.toUpperCase()}</p>
          <h3>{request.title}</h3>
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      <p className="sr-meta">{status.help} · Sent {formatDateTime(request.created_at) ?? "—"}</p>
      {request.description && <p className="sr-meta">You wrote: {request.description}</p>}
      {request.patient_message && (
        <p className="sr-message"><strong>Message from your care team:</strong> {request.patient_message}</p>
      )}

      {error && <p className="ws-callout ws-callout-error" role="alert">{error}</p>}

      {mode === "reply" && (
        <div className="ws-form">
          <div className="ws-field">
            <label htmlFor={replyId}>Your reply</label>
            <textarea
              disabled={busy}
              id={replyId}
              maxLength={MAX_INFO_MESSAGE}
              onChange={(event) => setReply(event.target.value)}
              rows={3}
              value={reply}
            />
          </div>
          <div className="ws-form-actions">
            <button className="ws-btn ws-btn-primary" disabled={busy} onClick={handleReply} type="button">
              {busy ? "Sending…" : "Send reply"}
            </button>
            <button className="ws-btn ws-btn-secondary" disabled={busy} onClick={() => setMode(null)} type="button">
              Not now
            </button>
          </div>
        </div>
      )}

      {mode === "cancel" && (
        <div className="ws-form-actions" role="group" aria-label="Confirm cancellation">
          <span>Cancel this request?</span>
          <button className="ws-btn ws-btn-danger" disabled={busy} onClick={() => run(() => cancelRequest(token, request.task_id))} type="button">
            {busy ? "Cancelling…" : "Yes, cancel it"}
          </button>
          <button className="ws-btn ws-btn-secondary" disabled={busy} onClick={() => setMode(null)} type="button">
            Keep it
          </button>
        </div>
      )}

      {mode === null && (request.can_add_info || request.can_cancel) && (
        <div className="sr-actions">
          {request.can_add_info && (
            <button className="ws-btn ws-btn-primary" onClick={() => setMode("reply")} type="button">Reply with information</button>
          )}
          {request.can_cancel && (
            <button className="ws-btn ws-btn-secondary" onClick={() => setMode("cancel")} type="button">Cancel request</button>
          )}
        </div>
      )}
    </li>
  );
}

/** Appointment requests live on the Appointments page when that feature is on. */
export function withoutAppointments(items, appointmentsOn) {
  return appointmentsOn ? items.filter((item) => item.category !== "appointment_scheduling") : items;
}

export default function PatientServiceRequestsPage({ embedded = false }) {
  const { session } = useOutletContext();
  const token = session.access_token;
  const categories = useRemote(() => listCategories(token), [token]);
  const requests = useRemote(() => listRequests(token), [token]);
  const unavailable = categories.status === "unavailable" || requests.status === "unavailable";
  const items = requests.status === "ready"
    ? sortRequests(withoutAppointments(requests.data, readFeatureFlags().appointments))
    : [];
  const replies = needsReplyCount(items);

  return (
    <>
      {!embedded && <PageHeader
        actions={<RefreshButton busy={requests.status === "loading"} label="Refresh" onClick={requests.reload} />}
        description="Ask your care team for help with appointments, supplies, test results and more. Medication, treatment and diagnosis requests are reviewed by a physician."
        eyebrow="YOUR CARE TEAM"
        icon="pending"
        title="Service Requests"
      />}

      {unavailable && (
        <p className="ws-callout ws-callout-info" role="status">
          Service requests are not available yet. Please check back soon. For anything urgent, contact your care team directly.
        </p>
      )}

      {!unavailable && replies > 0 && (
        <p className="ws-callout ws-callout-warning" role="status">
          {replies === 1 ? "One request needs" : `${replies} requests need`} a reply from you.
        </p>
      )}

      {!unavailable && (
        <div className="sr-grid">
          <Panel eyebrow="NEW REQUEST" icon="records" title="Ask for help" titleId="sr-new-title" tone={1}>
            {categories.status === "loading" && <LoadingMessage />}
            {categories.status === "error" && <ErrorMessage message={categories.error} onRetry={categories.reload} />}
            {categories.status === "ready" && (
              <NewRequestForm categories={categories.data} onSubmitted={requests.reload} token={token} />
            )}
          </Panel>

          <Panel eyebrow="YOUR REQUESTS" icon="history" subtitle="Newest first. Messages from your care team appear here." title="Your requests" titleId="sr-list-title" tone={2}>
            {requests.status === "loading" && !requests.data && <LoadingMessage />}
            {requests.status === "error" && <ErrorMessage message={requests.error} onRetry={requests.reload} />}
            {requests.status === "ready" && items.length === 0 && (
              <EmptyMessage>You haven&apos;t sent any requests yet.</EmptyMessage>
            )}
            {items.length > 0 && (
              <ul className="sr-list" aria-label="Your service requests">
                {items.map((request) => (
                  <RequestCard key={request.task_id} onChanged={requests.reload} request={request} token={token} />
                ))}
              </ul>
            )}
          </Panel>
        </div>
      )}
    </>
  );
}
