import { useCallback, useEffect, useRef, useState } from "react";
import { Outlet } from "react-router-dom";

import {
  apiBaseUrl,
  getApiJson,
  readApiResponse,
} from "../../shared/apiClient.js";
import { WorkspaceShell } from "../../shared/WorkspaceShell.jsx";
const adminPanelKeys = ["users", "permissions", "requests", "audit"];
const adminOverviewKeys = {
  users: "users",
  permissions: "permissions",
  requests: "ai_requests",
  audit: "audit_logs",
};

function adminPanelsFromOverview(overview) {
  return Object.fromEntries(
    adminPanelKeys.map((key) => {
      const overviewKey = adminOverviewKeys[key];
      return [
        key,
        {
          loading: false,
          data: overview[overviewKey] || [],
          error: overview.errors[overviewKey] || "",
        },
      ];
    }),
  );
}

/**
 * Admin layout route. State lives here so it persists while the Admin moves
 * between pages; each page reads it through the router outlet context.
 */
export default function AdminWorkspace({ user, session, onLogout, initialOverview, signOutError }) {
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const [panels, setPanels] = useState(() =>
    initialOverview
      ? adminPanelsFromOverview(initialOverview)
      : Object.fromEntries(adminPanelKeys.map((key) => [key, { loading: true, data: [], error: "" }])),
  );
  const [inviteForm, setInviteForm] = useState({ name: "", email: "", role: "patient" });
  const [inviteStatus, setInviteStatus] = useState("");
  const [inviteError, setInviteError] = useState("");
  const [inviting, setInviting] = useState(false);
  const [deactivationStatus, setDeactivationStatus] = useState("");
  const [deactivationError, setDeactivationError] = useState("");
  const [deactivatingUserId, setDeactivatingUserId] = useState(null);
  const [analysisQuestion, setAnalysisQuestion] = useState("");
  const [analysisAnswer, setAnalysisAnswer] = useState("");
  const [analysisError, setAnalysisError] = useState("");
  const [analysisRecordCount, setAnalysisRecordCount] = useState(null);
  const [analyzingRequests, setAnalyzingRequests] = useState(false);
  const [auditAnalysisQuestion, setAuditAnalysisQuestion] = useState("");
  const [auditAnalysisResult, setAuditAnalysisResult] = useState(null);
  const [auditAnalysisError, setAuditAnalysisError] = useState("");
  const [analyzingAuditLogs, setAnalyzingAuditLogs] = useState(false);

  const refreshPanels = useCallback(async () => {
    const accessToken = sessionRef.current?.access_token;
    if (!accessToken) return;

    setPanels(
      Object.fromEntries(
        adminPanelKeys.map((key) => [key, { loading: true, data: [], error: "" }]),
      ),
    );

    try {
      const result = await getApiJson("/admin/overview", accessToken);
      setPanels(adminPanelsFromOverview(result));
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not load this section.";
      setPanels(
        Object.fromEntries(
          adminPanelKeys.map((key) => [key, { loading: false, data: [], error: message }]),
        ),
      );
    }
  }, []);

  async function handleInvite(event) {
    event.preventDefault();
    setInviteStatus("");
    setInviteError("");
    setInviting(true);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/users/invite`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${sessionRef.current.access_token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(inviteForm),
      });
      const result = await readApiResponse(response);
      setInviteStatus(`Invitation sent to ${result.email}. They can set a password from the email link.`);
      setInviteForm({ name: "", email: "", role: "patient" });
      void refreshPanels();
    } catch (error) {
      setInviteError(
        error instanceof Error ? error.message : "Could not send the invitation.",
      );
    } finally {
      setInviting(false);
    }
  }

  async function handleDeactivate(record) {
    const confirmed = window.confirm(
      `Deactivate ${record.email}? They will lose access, but their account and historical records will be preserved.`,
    );
    if (!confirmed) return;

    const accessToken = sessionRef.current?.access_token;
    if (!accessToken) {
      setDeactivationError("Your session expired. Sign in again before deactivating an account.");
      return;
    }

    setDeactivationStatus("");
    setDeactivationError("");
    setDeactivatingUserId(record.user_id);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/users/${record.user_id}/deactivate`, {
        method: "POST",
        headers: { Authorization: "Bearer " + accessToken },
      });
      const result = await readApiResponse(response);
      setDeactivationStatus(`${record.email} has been deactivated. The account and its history were preserved.`);
      await refreshPanels();
    } catch (error) {
      setDeactivationError(
        error instanceof Error ? error.message : "Could not deactivate this account.",
      );
      void refreshPanels();
    } finally {
      setDeactivatingUserId(null);
    }
  }

  async function handleAnalyzeRequests(event) {
    event.preventDefault();
    setAnalysisError("");
    setAnalysisAnswer("");
    setAnalysisRecordCount(null);

    const accessToken = sessionRef.current?.access_token;
    if (!accessToken) {
      setAnalysisError("Your session expired. Sign in again before using AI analysis.");
      return;
    }

    setAnalyzingRequests(true);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/ai-requests/analyze`, {
        method: "POST",
        headers: {
          Authorization: "Bearer " + accessToken,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ question: analysisQuestion }),
      });
      const result = await readApiResponse(response);
      setAnalysisAnswer(result.answer);
      setAnalysisRecordCount(result.records_analyzed);
    } catch (error) {
      setAnalysisError(
        error instanceof Error ? error.message : "Could not analyze AI request activity.",
      );
    } finally {
      setAnalyzingRequests(false);
    }
  }

  async function handleAnalyzeAuditLogs(event) {
    event.preventDefault();
    setAuditAnalysisError("");
    setAuditAnalysisResult(null);

    const accessToken = sessionRef.current?.access_token;
    if (!accessToken) {
      setAuditAnalysisError("Your session expired. Sign in again before using audit analysis.");
      return;
    }

    setAnalyzingAuditLogs(true);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/audit-logs/analyze`, {
        method: "POST",
        headers: {
          Authorization: "Bearer " + accessToken,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ question: auditAnalysisQuestion }),
      });
      const result = await readApiResponse(response);
      setAuditAnalysisResult(result);
    } catch (error) {
      setAuditAnalysisError(
        error instanceof Error ? error.message : "Could not analyze audit log activity.",
      );
    } finally {
      setAnalyzingAuditLogs(false);
    }
  }

  useEffect(() => {
    if (!initialOverview) void refreshPanels();
  }, [initialOverview, refreshPanels]);

  const decisions = panels.requests.data.reduce(
    (counts, request) => ({
      ...counts,
      [request.guardrail_decision]: (counts[request.guardrail_decision] || 0) + 1,
    }),
    { Allow: 0, Block: 0, Escalate: 0 },
  );

  const outletContext = {
    user,
    panels,
    refreshPanels,
    decisions,
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
    analysisQuestion,
    setAnalysisQuestion,
    analysisAnswer,
    analysisError,
    analysisRecordCount,
    analyzingRequests,
    handleAnalyzeRequests,
    auditAnalysisQuestion,
    setAuditAnalysisQuestion,
    auditAnalysisResult,
    auditAnalysisError,
    analyzingAuditLogs,
    handleAnalyzeAuditLogs,
  };

  return (
    <WorkspaceShell
      account={{ initials: "A", name: "Administrator", detail: user.email }}
      alerts={signOutError && <p className="ws-alert" role="alert">{signOutError}</p>}
      onLogout={onLogout}
      role="admin"
      roleLabel="Admin"
      theme="admin"
      workspaceLabel="AI SAFETY COMMAND CENTER"
    >
      <Outlet context={outletContext} />
    </WorkspaceShell>
  );
}