import { useOutletContext } from "react-router-dom";

import { roles } from "../../shared/apiClient.js";
import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import {
  Badge,
  Chip,
  ErrorMessage,
  LoadingMessage,
  ModuleLink,
  Panel,
  RefreshButton,
  StatCard,
} from "../../shared/workspaceUi.jsx";

function PanelState({ state, onRetry }) {
  if (state.loading) return <LoadingMessage />;
  if (state.error) return <ErrorMessage message={state.error} onRetry={onRetry} />;
  if (!state.data?.length) return <p className="ws-message">No records to display.</p>;
  return null;
}

function hasRows(state) {
  return !state.loading && !state.error && state.data.length > 0;
}

function formatTimestamp(value) {
  return value ? new Date(value).toLocaleString() : "—";
}

function RolePermissionMatrix({ rows }) {
  const roleNames = ["Admin", "Patient", "Physician", "Technician"];
  const actions = [...new Set(rows.map((row) => row.action))].sort();
  const permissionMap = new Map(
    rows.map((row) => [`${row.role}:${row.action}`, row.allowed]),
  );

  return (
    <div className="ws-table-wrap">
      <table className="ws-table ws-matrix">
        <thead>
          <tr>
            <th scope="col">Action</th>
            {roleNames.map((role) => <th scope="col" key={role}>{role}</th>)}
          </tr>
        </thead>
        <tbody>
          {actions.map((action) => (
            <tr key={action}>
              <th scope="row">{action.replaceAll("_", " ")}</th>
              {roleNames.map((role) => {
                const allowed = permissionMap.get(`${role}:${action}`) === true;
                return (
                  <td key={role}>
                    <span
                      className={`ws-permission ${allowed ? "is-allowed" : "is-denied"}`}
                      aria-label={allowed ? "Allowed" : "Not allowed"}
                    >
                      {allowed ? "Allowed" : "—"}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CommandArt() {
  return (
    <div className="ws-hero-art" aria-hidden="true">
      <span className="ws-hero-ring ws-hero-ring-outer" />
      <span className="ws-hero-ring ws-hero-ring-inner" />
      <svg viewBox="0 0 240 220" fill="none">
        <path className="ws-hero-line" d="M39 55 82 82m76-31-39 31m55 67-40-27m-83 34 44-34" />
        <path className="ws-hero-glyph" d="M120 38 166 57v36c0 31-18 55-46 69-28-14-46-38-46-69V57l46-19Z" />
        <path className="ws-hero-mark" d="m101 96 13 13 27-29" />
        <circle className="ws-hero-node ws-node-1" cx="38" cy="52" r="8" />
        <circle className="ws-hero-node ws-node-2" cx="164" cy="49" r="9" />
        <circle className="ws-hero-node ws-node-3" cx="193" cy="159" r="7" />
        <circle className="ws-hero-node ws-node-4" cx="57" cy="166" r="8" />
      </svg>
    </div>
  );
}

export function AdminOverviewPage() {
  const { panels, refreshPanels, decisions } = useOutletContext();
  const anyLoading = Object.values(panels).some((panel) => panel.loading);

  return (
    <>
      <section className="ws-hero" aria-labelledby="ws-page-title">
        <div className="ws-hero-copy">
          <p className="ws-eyebrow">AI SAFETY COMMAND CENTER</p>
          <h1 id="ws-page-title" tabIndex={-1}>Pre-execution safety overview</h1>
          <p className="ws-hero-kicker">
            Safeguarding Autonomous Agentic AI in Clinical Workflows Through
            Pre-Execution Action Authorization
          </p>
          <p>Review access, AI guardrail decisions, and audit activity in one privacy-conscious workspace.</p>
          <button className="ws-btn ws-btn-hero" disabled={anyLoading} onClick={() => void refreshPanels()} type="button">
            <span aria-hidden="true">↻</span> {anyLoading ? "Refreshing…" : "Refresh data"}
          </button>
        </div>
        <CommandArt />
      </section>

      <div className="ws-stat-grid" aria-label="Recent activity summary">
        <StatCard
          icon="users"
          label="APPLICATION USERS"
          note="Accounts in this page"
          tone={1}
          value={panels.users.loading ? "—" : panels.users.data.length}
        />
        <StatCard
          icon="guardrails"
          label="AI REQUESTS"
          note="Most recent requests"
          tone={2}
          value={panels.requests.loading ? "—" : panels.requests.data.length}
        />
        <StatCard
          icon="escalate"
          label="ESCALATED"
          note="In the displayed requests"
          tone={3}
          value={panels.requests.loading ? "—" : decisions.Escalate}
        />
        <StatCard
          icon="audit"
          label="AUDIT EVENTS"
          note="Most recent events"
          tone={4}
          value={panels.audit.loading ? "—" : panels.audit.data.length}
        />
      </div>

      <h2 className="ws-section-title">Command modules</h2>
      <div className="ws-module-grid">
        <ModuleLink
          description="Invite users and deactivate access while preserving account history."
          icon="users"
          title="User access"
          to="/dashboard/admin/users"
          tone={1}
        />
        <ModuleLink
          description="Current role permissions from the database. Read only."
          icon="permissions"
          title="Role permissions"
          to="/dashboard/admin/permissions"
          tone={2}
        />
        <ModuleLink
          description="Allow, Block and Escalate decisions, with AI analysis of request metadata."
          icon="guardrails"
          meta={panels.requests.loading ? null : `${decisions.Allow} allowed · ${decisions.Block} blocked · ${decisions.Escalate} escalated`}
          title="AI guardrails"
          to="/dashboard/admin/ai-requests"
          tone={3}
        />
        <ModuleLink
          description="Append-only security events, with verified counts and AI explanation."
          icon="audit"
          title="Audit trail"
          to="/dashboard/admin/audit-logs"
          tone={4}
        />
      </div>
    </>
  );
}

export function AdminUsersPage() {
  const {
    user,
    panels,
    refreshPanels,
    inviteForm,
    setInviteForm,
    inviteStatus,
    inviteError,
    inviting,
    handleInvite,
    deactivationStatus,
    deactivationError,
    deactivatingUserId,
    handleDeactivate,
  } = useOutletContext();

  return (
    <>
      <PageHeader
        actions={<RefreshButton busy={panels.users.loading} onClick={() => void refreshPanels()} />}
        description="Invite users and deactivate access while preserving account history."
        eyebrow="ACCOUNT DIRECTORY"
        icon="users"
        title="User access"
      />
      <Panel
        aside={<Chip>History preserved</Chip>}
        eyebrow="ACCOUNT DIRECTORY"
        icon="users"
        subtitle="New accounts receive an email invitation to set their password."
        title="User management"
        titleId="admin-users-title"
        tone={1}
      >
        <form className="ws-form ws-invite-form" onSubmit={handleInvite}>
          <label className="ws-field">
            Name
            <input
              autoComplete="name"
              maxLength={120}
              onChange={(event) => setInviteForm((current) => ({ ...current, name: event.target.value }))}
              required
              value={inviteForm.name}
            />
          </label>
          <label className="ws-field">
            Email
            <input
              autoComplete="email"
              onChange={(event) => setInviteForm((current) => ({ ...current, email: event.target.value }))}
              required
              type="email"
              value={inviteForm.email}
            />
          </label>
          <label className="ws-field">
            Assigned role
            <select
              onChange={(event) => setInviteForm((current) => ({ ...current, role: event.target.value }))}
              value={inviteForm.role}
            >
              {roles.map((role) => (
                <option key={role} value={role}>
                  {role.charAt(0).toUpperCase() + role.slice(1)}
                </option>
              ))}
            </select>
          </label>
          <button className="ws-btn ws-btn-primary" disabled={inviting} type="submit">
            {inviting ? "Sending invitation…" : "Invite user"}
          </button>
        </form>
        {inviteStatus && <p className="ws-success" role="status">{inviteStatus}</p>}
        {inviteError && <p className="ws-alert" role="alert">{inviteError}</p>}
        {deactivationStatus && <p className="ws-success" role="status">{deactivationStatus}</p>}
        {deactivationError && <p className="ws-alert" role="alert">{deactivationError}</p>}
        <PanelState state={panels.users} onRetry={() => void refreshPanels()} />
        {hasRows(panels.users) && (
          <div className="ws-table-wrap">
            <table className="ws-table">
              <thead><tr><th scope="col">Name</th><th scope="col">Email</th><th scope="col">Role</th><th scope="col">Status</th><th scope="col">Account access</th></tr></thead>
              <tbody>
                {panels.users.data.map((record) => (
                  <tr key={record.user_id}>
                    <td>{record.name}</td>
                    <td>{record.email}</td>
                    <td><Badge tone="role">{record.role}</Badge></td>
                    <td>
                      <Badge tone={record.is_active ? "active" : "inactive"}>
                        {record.is_active ? "Active" : "Deactivated"}
                      </Badge>
                    </td>
                    <td>
                      {record.user_id === user.user_id ? (
                        <Chip>Current account</Chip>
                      ) : record.is_active ? (
                        <button
                          className="ws-btn ws-btn-danger ws-btn-small"
                          disabled={deactivatingUserId !== null}
                          onClick={() => void handleDeactivate(record)}
                          type="button"
                        >
                          {deactivatingUserId === record.user_id ? "Deactivating…" : "Deactivate"}
                        </button>
                      ) : (
                        <span className="ws-muted">Access removed</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}

export function AdminPermissionsPage() {
  const { panels, refreshPanels } = useOutletContext();

  return (
    <>
      <PageHeader
        actions={<RefreshButton busy={panels.permissions.loading} onClick={() => void refreshPanels()} />}
        description="Current permissions from the database. This matrix cannot be edited here."
        eyebrow="ACCESS CONTROL"
        icon="permissions"
        title="Role permissions"
      />
      <Panel
        aside={<Chip>Read only</Chip>}
        eyebrow="ACCESS CONTROL"
        icon="permissions"
        subtitle="Each AI action is checked against this matrix before it can run."
        title="Permission matrix"
        titleId="admin-permissions-title"
        tone={2}
      >
        <PanelState state={panels.permissions} onRetry={() => void refreshPanels()} />
        {hasRows(panels.permissions) && <RolePermissionMatrix rows={panels.permissions.data} />}
        <p className="ws-note">
          Admin is not permitted to view patient medical records. Role permissions do not grant unrestricted patient-data access.
        </p>
      </Panel>
    </>
  );
}

export function AdminAiRequestsPage() {
  const {
    panels,
    refreshPanels,
    analysisQuestion,
    setAnalysisQuestion,
    analysisAnswer,
    analysisError,
    analysisRecordCount,
    analyzingRequests,
    handleAnalyzeRequests,
  } = useOutletContext();

  return (
    <>
      <PageHeader
        actions={<RefreshButton busy={panels.requests.loading} onClick={() => void refreshPanels()} />}
        description="Request metadata and decisions only; clinical request content is excluded."
        eyebrow="GUARDRAIL ACTIVITY"
        icon="guardrails"
        title="AI guardrails"
      />
      <Panel
        aside={<Chip>Read only</Chip>}
        eyebrow="GUARDRAIL ACTIVITY"
        icon="guardrails"
        subtitle="Recent AI requests and the decision the safety framework made for each."
        title="AI request monitoring"
        titleId="admin-ai-title"
        tone={3}
      >
        <div className="ws-legend">
          <span><i className="ws-dot-allow" /> Allow</span>
          <span><i className="ws-dot-block" /> Block</span>
          <span><i className="ws-dot-escalate" /> Escalate</span>
        </div>
        <form className="ws-form ws-ai-form" onSubmit={handleAnalyzeRequests}>
          <label htmlFor="ai-request-question">Ask AI about request decisions</label>
          <div className="ws-ai-controls">
            <input
              id="ai-request-question"
              maxLength={500}
              onChange={(event) => setAnalysisQuestion(event.target.value)}
              placeholder="e.g. Summarize the blocked requests"
              required
              value={analysisQuestion}
            />
            <button
              className="ws-btn ws-btn-primary"
              disabled={analyzingRequests || analysisQuestion.trim().length < 3}
              type="submit"
            >
              {analyzingRequests ? "Analyzing…" : "Ask AI"}
            </button>
          </div>
          <p className="ws-form-note">
            Uses the latest 100 request IDs, actions, decisions, and timestamps only. Request content and block rationales are not provided to AI.
          </p>
        </form>
        {analysisError && <p className="ws-alert" role="alert">{analysisError}</p>}
        {analysisAnswer && (
          <div className="ws-answer" role="status">
            <p>{analysisAnswer}</p>
            <span>Based on {analysisRecordCount} recent request record(s).</span>
          </div>
        )}
        <PanelState state={panels.requests} onRetry={() => void refreshPanels()} />
        {hasRows(panels.requests) && (
          <div className="ws-table-wrap">
            <table className="ws-table">
              <thead><tr><th scope="col">Request</th><th scope="col">Action</th><th scope="col">Guardrail decision</th><th scope="col">Created</th></tr></thead>
              <tbody>
                {panels.requests.data.map((request) => (
                  <tr key={request.request_id}>
                    <td>#{request.request_id}</td>
                    <td>{request.action}</td>
                    <td><Badge tone={request.guardrail_decision.toLowerCase()}>{request.guardrail_decision}</Badge></td>
                    <td>{formatTimestamp(request.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}

export function AdminAuditLogsPage() {
  const {
    panels,
    refreshPanels,
    auditAnalysisQuestion,
    setAuditAnalysisQuestion,
    auditAnalysisResult,
    auditAnalysisError,
    analyzingAuditLogs,
    handleAnalyzeAuditLogs,
  } = useOutletContext();

  return (
    <>
      <PageHeader
        actions={<RefreshButton busy={panels.audit.loading} onClick={() => void refreshPanels()} />}
        description="Append-only event metadata. Free-text details are intentionally withheld."
        eyebrow="SECURITY"
        icon="audit"
        title="Audit trail"
      />
      <Panel
        aside={<Chip>Read only</Chip>}
        eyebrow="SECURITY"
        icon="audit"
        subtitle="Every account change and AI decision is recorded here."
        title="Audit log"
        titleId="admin-audit-title"
        tone={4}
      >
        <form className="ws-form ws-ai-form" onSubmit={handleAnalyzeAuditLogs}>
          <label htmlFor="audit-log-question">Ask AI about Audit Logs</label>
          <div className="ws-ai-controls">
            <input
              id="audit-log-question"
              maxLength={500}
              onChange={(event) => setAuditAnalysisQuestion(event.target.value)}
              placeholder="e.g. Count recent events by type and summarize what was recorded"
              required
              value={auditAnalysisQuestion}
            />
            <button
              className="ws-btn ws-btn-primary"
              disabled={analyzingAuditLogs || auditAnalysisQuestion.trim().length < 3}
              type="submit"
            >
              {analyzingAuditLogs ? "Analyzing…" : "Ask AI"}
            </button>
          </div>
          <p className="ws-form-note">
            Analyzes authorized event metadata only. User identities, free-text details, and patient information are excluded.
          </p>
        </form>
        {analyzingAuditLogs && (
          <p className="ws-message ws-message-loading" role="status" aria-live="polite">
            <span className="ws-spinner" aria-hidden="true" />
            Analyzing audit metadata with the AI model…
          </p>
        )}
        {auditAnalysisError && <p className="ws-alert" role="alert">{auditAnalysisError}</p>}
        {auditAnalysisResult && (
          <div className="ws-audit-result" role="region" aria-label="Audit analysis results">
            {auditAnalysisResult.records_analyzed === 0 ? (
              <p className="ws-message" role="status">
                No audit records were available for this analysis.
              </p>
            ) : (
              <>
                <section className="ws-count-card" aria-labelledby="verified-event-counts-title">
                  <div className="ws-result-heading">
                    <h3 id="verified-event-counts-title">Verified event counts</h3>
                    <span>{auditAnalysisResult.records_analyzed} records · limit {auditAnalysisResult.record_limit}</span>
                  </div>
                  {Object.keys(auditAnalysisResult.event_type_counts || {}).length > 0 ? (
                    <ul className="ws-count-list">
                      {Object.entries(auditAnalysisResult.event_type_counts)
                        .sort(([left], [right]) => left.localeCompare(right))
                        .map(([eventType, count]) => (
                          <li key={eventType}>
                            <span>{eventType.replaceAll("_", " ").replaceAll(".", " · ")}</span>
                            <strong>{count}</strong>
                          </li>
                        ))}
                    </ul>
                  ) : (
                    <p className="ws-message">No event categories were returned.</p>
                  )}
                </section>
                <section className="ws-answer ws-explanation" aria-labelledby="audit-ai-explanation-title">
                  <h3 id="audit-ai-explanation-title">AI explanation</h3>
                  <p>{auditAnalysisResult.answer}</p>
                </section>
              </>
            )}
          </div>
        )}
        <PanelState state={panels.audit} onRetry={() => void refreshPanels()} />
        {hasRows(panels.audit) && (
          <div className="ws-table-wrap">
            <table className="ws-table">
              <thead><tr><th scope="col">Event</th><th scope="col">Type</th><th scope="col">Request</th><th scope="col">Created</th></tr></thead>
              <tbody>
                {panels.audit.data.map((event) => (
                  <tr key={event.log_id}>
                    <td>#{event.log_id}</td>
                    <td>{event.event_type}</td>
                    <td>{event.request_id ? `#${event.request_id}` : "—"}</td>
                    <td>{formatTimestamp(event.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
