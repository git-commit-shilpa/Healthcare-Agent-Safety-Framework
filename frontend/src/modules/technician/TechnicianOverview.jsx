import { Link, useOutletContext } from "react-router-dom";

import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import { useRemote } from "../../shared/useRemote.js";
import {
  Badge,
  EmptyMessage,
  ErrorMessage,
  LoadingMessage,
  Panel,
  RefreshButton,
  StatCard,
} from "../../shared/workspaceUi.jsx";
import { getDashboard, listPatients } from "./technicianApi.js";
import { categoryLabel, formatDateTime, statusMeta } from "./technician.js";

function value(remote, pick) {
  if (remote.status === "ready") return pick(remote.data);
  return "—";
}

export default function TechnicianOverviewPage() {
  const { user, session } = useOutletContext();
  const token = session.access_token;
  const dashboard = useRemote(() => getDashboard(token), [token]);
  const patients = useRemote(() => listPatients(token), [token]);
  const firstName = (user.name || "there").trim().split(/\s+/)[0];

  const dashboardNote =
    dashboard.status === "loading"
      ? "Loading…"
      : dashboard.status === "unavailable"
        ? "Not enabled yet"
        : dashboard.status === "error"
          ? "Could not load"
          : null;
  const assigned = patients.status === "ready" ? patients.data.length : dashboard.status === "ready" ? dashboard.data.assigned_patients : "—";

  return (
    <>
      <PageHeader
        actions={<RefreshButton busy={dashboard.status === "loading"} onClick={() => { dashboard.reload(); patients.reload(); }} />}
        description="Your assigned patients and the requests waiting for you. Clinical decisions always stay with the physician."
        eyebrow="CARE OPERATIONS"
        icon="overview"
        title={`Welcome, ${firstName}.`}
      />

      {dashboard.status === "unavailable" && (
        <p className="ws-callout ws-callout-info" role="status">
          Task management is not switched on for this environment yet. Your assigned patients are still available.
        </p>
      )}

      <div className="ws-stat-grid">
        <StatCard icon="users" label="Assigned patients" note="Patients you can view" tone={1} value={assigned} />
        <StatCard icon="pending" label="Pending review" note={dashboardNote ?? "New and in review"} tone={2} value={value(dashboard, (d) => d.pending_review)} />
        <StatCard icon="pulse" label="In progress" note={dashboardNote ?? "Being handled"} tone={3} value={value(dashboard, (d) => d.in_progress)} />
        <StatCard icon="escalate" label="Awaiting physician" note={dashboardNote ?? "Approval requested"} tone={4} value={value(dashboard, (d) => d.awaiting_physician)} />
        <StatCard icon="records" label="Completed" note={dashboardNote ?? "Finished requests"} tone={1} value={value(dashboard, (d) => d.completed)} />
      </div>

      <Panel
        aside={<Link className="ws-btn ws-btn-secondary" to="/dashboard/technician/tasks">Open tasks</Link>}
        eyebrow="RECENT ACTIVITY"
        icon="history"
        subtitle="The last tasks that changed."
        title="Recent tasks"
        titleId="recent-tasks-title"
        tone={2}
      >
        {dashboard.status === "loading" && <LoadingMessage />}
        {dashboard.status === "error" && <ErrorMessage message={dashboard.error} onRetry={dashboard.reload} />}
        {dashboard.status === "unavailable" && <EmptyMessage>Recent tasks will appear here once task management is enabled.</EmptyMessage>}
        {dashboard.status === "ready" && dashboard.data.recent_tasks.length === 0 && (
          <EmptyMessage>No tasks have been assigned to you yet.</EmptyMessage>
        )}
        {dashboard.status === "ready" && dashboard.data.recent_tasks.length > 0 && (
          <div className="ws-table-wrap">
            <table className="ws-table">
              <caption className="ws-visually-hidden">Your most recently updated tasks</caption>
              <thead>
                <tr>
                  <th scope="col">Patient</th>
                  <th scope="col">Request</th>
                  <th scope="col">Status</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {dashboard.data.recent_tasks.map((task) => {
                  const status = statusMeta(task.status);
                  return (
                    <tr key={task.task_id}>
                      <td>{task.patient.name ?? `Patient ${task.patient.patient_id}`}</td>
                      <td>{categoryLabel(task.category)}: {task.title}</td>
                      <td><Badge tone={status.tone}>{status.label}</Badge></td>
                      <td>{formatDateTime(task.updated_at) ?? "—"}</td>
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
