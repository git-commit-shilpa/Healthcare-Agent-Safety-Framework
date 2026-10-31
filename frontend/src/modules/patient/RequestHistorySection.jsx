import { Link, useOutletContext } from "react-router-dom";

import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import {
  Badge,
  ErrorMessage,
  LoadingMessage,
  Panel,
  RefreshButton,
} from "../../shared/workspaceUi.jsx";
import {
  describeApprovalStatus,
  describeDecision,
  formatDateTime,
} from "./patientCorrection.js";

export default function PatientHistoryPage() {
  const { historyState, refreshHistory } = useOutletContext();
  const refreshing = historyState.status === "loading";

  return (
    <>
      <PageHeader
        actions={
          historyState.status !== "forbidden" && (
            <RefreshButton busy={refreshing} label="Refresh" onClick={refreshHistory} />
          )
        }
        description="Each correction you submit is escalated by the safety framework to your assigned Physician. Follow its review here."
        eyebrow="YOUR ACTIVITY"
        icon="history"
        title="AI Request History"
      />

      <Panel
        eyebrow="CORRECTION REQUESTS"
        icon="history"
        subtitle="Newest first. Your proposed text is kept for your Physician's review and is not shown here or in Admin monitoring."
        title="Your requests"
        titleId="history-title"
        tone={2}
      >
        <div className="ws-legend" aria-label="Status legend">
          <span><i className="ws-dot-escalate" /> Escalated for Physician review</span>
          <span><i className="ws-dot-pending" /> Pending review</span>
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
                  <th scope="col">Guardrail decision</th>
                  <th scope="col">Physician review</th>
                  <th scope="col">Submitted</th>
                  <th scope="col">Reviewed</th>
                </tr>
              </thead>
              <tbody>
                {historyState.items.map((item) => {
                  const decision = describeDecision(item.guardrail_decision);
                  const approval = describeApprovalStatus(item);
                  return (
                    <tr key={item.request_id}>
                      <td>#{item.request_id}</td>
                      <td>Treatment correction</td>
                      <td><Badge tone={decision.tone}>{decision.label}</Badge></td>
                      <td><Badge tone={approval.tone}>{approval.label}</Badge></td>
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
    </>
  );
}
