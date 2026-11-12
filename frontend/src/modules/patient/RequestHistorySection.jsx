import { useId, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { describeError } from "../../shared/taskApi.js";

import {
  Badge,
  ErrorMessage,
  LoadingMessage,
  Panel,
} from "../../shared/workspaceUi.jsx";
import {
  describeApprovalStatus,
  formatDateTime,
  validateCorrectionReply,
  MAX_CORRECTION_REPLY,
} from "./patientCorrection.js";
import { addRequestInfo } from "./serviceRequestsApi.js";

/** The care team's question about a correction, and the patient's answer. Replies are short and not for medical detail. */
function CoordinationReply({ coordination, token, onDone }) {
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const id = useId();

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current) return;
    const problem = validateCorrectionReply(reply);
    if (problem) {
      setError(problem);
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await addRequestInfo(token, coordination.task_id, reply);
      setReply("");
      await onDone();
    } catch (requestError) {
      setError(describeError(requestError));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <form className="ws-form" noValidate onSubmit={handleSubmit}>
      <div className="ws-field">
        <label htmlFor={`${id}-reply`}>Your reply to your care team</label>
        <p className="ws-form-note">
          Please do not include medical details or repeat your correction. Your care team and your Physician can read this reply.
        </p>
        <textarea
          aria-invalid={Boolean(error)}
          disabled={busy}
          id={`${id}-reply`}
          maxLength={MAX_CORRECTION_REPLY}
          onChange={(event) => { setReply(event.target.value); setError(""); }}
          rows={3}
          value={reply}
        />
        <div className="ws-field-meta"><span>{reply.length}/{MAX_CORRECTION_REPLY}</span></div>
        {error && <p className="ws-field-error" role="alert">{error}</p>}
      </div>
      <div className="ws-form-actions">
        <button className="ws-btn ws-btn-primary" disabled={busy} type="submit">{busy ? "Sending…" : "Send reply"}</button>
      </div>
    </form>
  );
}

/** Medical-record correction requests (stored separately from service requests). */
export function CorrectionRequestsPanel({ historyState, refreshHistory, token }) {
  return (
      <Panel
        eyebrow="MEDICAL RECORD CORRECTIONS"
        icon="history"
        subtitle="Newest first. Your proposed text is kept for your Physician's review and is not shown here or in Admin monitoring."
        title="Record correction requests"
        titleId="history-title"
        tone={2}
      >
        <div className="ws-legend" aria-label="Status legend">
          <span><i className="ws-dot-pending" /> Pending Physician review</span>
          <span><i className="ws-dot-allow" /> Approved</span>
          <span><i className="ws-dot-block" /> Rejected</span>
          <span><i className="ws-dot-issue" /> Needs attention</span>
        </div>

        {historyState.status === "forbidden" && (
          <p className="ws-message" role="status">Request history is not available for your account.</p>
        )}
        {historyState.status === "loading" && <LoadingMessage />}
        {historyState.status === "error" && (
          <ErrorMessage message={historyState.error} onRetry={refreshHistory} />
        )}
        {historyState.status === "ready" && historyState.items.length === 0 && (
          <p className="ws-message" role="status">
            You haven&apos;t submitted any correction requests. Requests you submit from{" "}
            <Link className="ws-link-button" to="/dashboard/patient/records">My Medical Records</Link>{" "}
            will appear here.
          </p>
        )}
        {historyState.status === "ready" && historyState.items.length > 0 && (
          <div className="ws-table-wrap">
            <table className="ws-table">
              <caption className="ws-visually-hidden">
                Your treatment correction requests, newest first
              </caption>
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col">Type</th>
                  <th scope="col">Physician review</th>
                  <th scope="col">Care team</th>
                  <th scope="col">Submitted</th>
                  <th scope="col">Reviewed</th>
                </tr>
              </thead>
              <tbody>
                {historyState.items.map((item) => {
                  const approval = describeApprovalStatus(item);
                  return (
                    <tr key={item.request_id}>
                      <td>#{item.request_id}</td>
                      <td>Record correction</td>
                      <td><Badge tone={approval.tone}>{approval.label}</Badge></td>
                      <td>
                        {item.coordination ? (
                          <>
                            <p>{item.coordination.status_label}</p>
                            {item.coordination.patient_message && item.coordination.patient_message !== item.coordination.status_label && (
                              <p className="ws-form-note">{item.coordination.patient_message}</p>
                            )}
                            {item.coordination.can_add_info && token && (
                              <CoordinationReply coordination={item.coordination} onDone={refreshHistory} token={token} />
                            )}
                          </>
                        ) : "—"}
                      </td>
                      <td>{formatDateTime(item.created_at) ?? "—"}</td>
                      <td>{formatDateTime(item.reviewed_at) ?? "Not yet reviewed"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
  );
}
