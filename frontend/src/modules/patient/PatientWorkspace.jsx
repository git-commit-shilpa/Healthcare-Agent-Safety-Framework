import { useCallback, useEffect, useState } from "react";
import { Outlet } from "react-router-dom";

import { getApiJson } from "../../shared/apiClient.js";
import { WorkspaceShell } from "../../shared/WorkspaceShell.jsx";
import { getCorrectionRequests, getPatientRecord } from "./patientApi.js";
import { parseHistoryResponse, parseRecordResponse } from "./patientCorrection.js";

function errorMessage(error, fallback) {
  return error instanceof Error && error.message ? error.message : fallback;
}

/**
 * Patient layout route. Record and request-history state live here so every
 * Patient page shares one copy and it survives navigation between pages.
 */
export default function PatientWorkspace({ user, session, onLogout, signOutError }) {
  const [dashboardError, setDashboardError] = useState("");
  const [recordState, setRecordState] = useState({ status: "loading", record: null, error: "" });
  const [recordRetry, setRecordRetry] = useState(0);
  const canViewHistory = user.permissions.includes("view_own_correction_requests");
  const [historyState, setHistoryState] = useState(
    canViewHistory
      ? { status: "loading", items: [], error: "" }
      : { status: "forbidden", items: [], error: "" },
  );
  const [historyRetry, setHistoryRetry] = useState(0);
  const initials = user.name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();

  // Same role-dashboard check the app performed before the redesign.
  useEffect(() => {
    let active = true;
    setDashboardError("");
    getApiJson("/dashboard/patient", session.access_token).catch((requestError) => {
      if (active) {
        setDashboardError(
          requestError instanceof Error ? requestError.message : "Could not load the dashboard.",
        );
      }
    });
    return () => {
      active = false;
    };
  }, [session.user.id]);

  useEffect(() => {
    let active = true;
    setRecordState({ status: "loading", record: null, error: "" });
    getPatientRecord(session.access_token)
      .then((result) => {
        const record = parseRecordResponse(result);
        if (active) {
          setRecordState({ status: record === null ? "empty" : "ready", record, error: "" });
        }
      })
      .catch((error) => {
        if (active) {
          setRecordState({
            status: "error",
            record: null,
            error: errorMessage(error, "Could not load your medical record."),
          });
        }
      });
    return () => {
      active = false;
    };
  }, [recordRetry, session.access_token]);

  useEffect(() => {
    if (!canViewHistory) return undefined;
    let active = true;
    setHistoryState({ status: "loading", items: [], error: "" });
    getCorrectionRequests(session.access_token)
      .then((result) => {
        const items = parseHistoryResponse(result);
        if (active) setHistoryState({ status: "ready", items, error: "" });
      })
      .catch((error) => {
        if (active) {
          setHistoryState({
            status: "error",
            items: [],
            error: errorMessage(error, "Could not load your correction requests."),
          });
        }
      });
    return () => {
      active = false;
    };
  }, [canViewHistory, historyRetry, session.access_token]);

  const retryRecord = useCallback(() => setRecordRetry((current) => current + 1), []);
  const refreshHistory = useCallback(() => setHistoryRetry((current) => current + 1), []);

  const alerts = (
    <>
      {dashboardError && <p className="ws-alert" role="alert">{dashboardError}</p>}
      {signOutError && <p className="ws-alert" role="alert">{signOutError}</p>}
    </>
  );

  return (
    <WorkspaceShell
      account={{ initials, name: user.name, detail: user.email }}
      alerts={alerts}
      onLogout={onLogout}
      role="patient"
      roleLabel="Patient"
      theme="patient"
      workspaceLabel="YOUR CARE WORKSPACE"
    >
      <Outlet
        context={{
          user,
          session,
          recordState,
          historyState,
          retryRecord,
          refreshHistory,
        }}
      />
    </WorkspaceShell>
  );
}
