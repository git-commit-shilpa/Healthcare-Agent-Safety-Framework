import { Outlet } from "react-router-dom";

import { WorkspaceShell } from "../../shared/WorkspaceShell.jsx";

/** Technician layout route: shared shell, Technician navigation and theme. */
export default function TechnicianWorkspace({ user, session, onLogout, signOutError }) {
  const displayName = user.name || user.email;
  const initials = displayName
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase();

  const alerts = signOutError ? (
    <p className="ws-alert" role="alert">{signOutError}</p>
  ) : null;

  return (
    <WorkspaceShell
      account={{ initials, name: displayName, detail: user.email }}
      alerts={alerts}
      onLogout={onLogout}
      role="technician"
      roleLabel="Technician"
      theme="technician"
      workspaceLabel="CARE OPERATIONS"
    >
      <Outlet context={{ user, session }} />
    </WorkspaceShell>
  );
}
