import { useState } from "react";
import { useOutletContext } from "react-router-dom";

import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import { useRemote } from "../../shared/useRemote.js";
import {
  Badge,
  EmptyMessage,
  ErrorMessage,
  LoadingMessage,
  Panel,
  RefreshButton,
} from "../../shared/workspaceUi.jsx";
import TaskDetailPanel from "./TaskDetailPanel.jsx";
import { listTasks } from "./technicianApi.js";
import { readFeatureFlags } from "../../app/features.js";
import { TASK_GROUPS, categoryLabel, filterTasks, groupCounts, scopeTasks, statusMeta } from "./technician.js";

const COPY = {
  tasks: {
    title: "Tasks",
    icon: "pending",
    description: "Requests from your assigned patients. Choose ALLOW, BLOCK or HOLD; the safety framework checks every decision, and clinical requests always need a physician.",
    empty: "No requests have been assigned to you yet.",
  },
  scheduling: {
    title: "Scheduling Requests",
    icon: "calendar",
    description: "Appointment requests from your assigned patients. Confirm the appointment, reject it with a reason, or ask the patient for more information.",
    empty: "No appointment requests have been assigned to you yet.",
  },
};

export default function TechnicianTasksPage({ scope = "tasks" }) {
  const copy = COPY[scope] ?? COPY.tasks;
  const appointmentsOn = readFeatureFlags().appointments;
  const { session } = useOutletContext();
  const token = session.access_token;
  const tasks = useRemote(() => listTasks(token), [token]);
  const [group, setGroup] = useState("needs_review");
  const [selectedId, setSelectedId] = useState(null);

  const items = scopeTasks(tasks.data?.items ?? [], scope, appointmentsOn);
  const visible = filterTasks(items, group);
  const counts = groupCounts(items);

  return (
    <>
      <PageHeader
        actions={<RefreshButton busy={tasks.status === "loading"} label="Refresh" onClick={tasks.reload} />}
        description={copy.description}
        eyebrow="CARE OPERATIONS"
        icon={copy.icon}
        title={copy.title}
      />

      {tasks.status === "unavailable" && (
        <p className="ws-callout ws-callout-info" role="status">
          Task management is not switched on for this environment yet. Nothing is wrong with your account.
        </p>
      )}

      <div className="tc-layout">
        <Panel eyebrow="YOUR QUEUE" icon="pending" title="Requests" titleId="task-list-title" tone={2}>
          <div className="tc-tabs" role="tablist" aria-label="Filter requests">
            {TASK_GROUPS.map((entry) => (
              <button
                aria-selected={group === entry.id}
                className={`tc-tab${group === entry.id ? " is-active" : ""}`}
                key={entry.id}
                onClick={() => setGroup(entry.id)}
                role="tab"
                type="button"
              >
                {entry.label} <span className="tc-count">{counts[entry.id]}</span>
              </button>
            ))}
          </div>

          {tasks.status === "loading" && !tasks.data && <LoadingMessage />}
          {tasks.status === "error" && <ErrorMessage message={tasks.error} onRetry={tasks.reload} />}
          {tasks.status === "ready" && items.length === 0 && (
            <EmptyMessage>{copy.empty}</EmptyMessage>
          )}
          {tasks.status === "ready" && items.length > 0 && visible.length === 0 && (
            <EmptyMessage>No requests in this view.</EmptyMessage>
          )}
          {visible.length > 0 && (
            <ul className="tc-list" aria-label="Requests">
              {visible.map((task) => {
                const status = statusMeta(task.status);
                return (
                  <li key={task.task_id}>
                    <button
                      aria-current={selectedId === task.task_id ? "true" : undefined}
                      className={`tc-item${selectedId === task.task_id ? " is-selected" : ""}`}
                      onClick={() => setSelectedId(task.task_id)}
                      type="button"
                    >
                      <span className="tc-item-top">
                        <strong>{task.title}</strong>
                        <Badge tone={status.tone}>{status.label}</Badge>
                      </span>
                      <span className="tc-item-meta">
                        {task.patient.name ?? `Patient ${task.patient.patient_id}`} · {categoryLabel(task.category)}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        <Panel eyebrow="REQUEST" icon="records" title="Details" titleId="task-detail-title" tone={1}>
          {selectedId === null ? (
            <EmptyMessage>Select a request to review it.</EmptyMessage>
          ) : (
            <TaskDetailPanel key={selectedId} onChanged={tasks.reload} taskId={selectedId} token={token} />
          )}
        </Panel>
      </div>
    </>
  );
}
