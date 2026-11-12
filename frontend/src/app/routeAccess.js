import { readFeatureFlags } from "./features.js";

/** Pages shown in each role's sidebar. Paths are relative to /dashboard/<role>. */
export function buildWorkspacePages(flags = {}) {
  return {
    admin: [
      { path: "overview", label: "Overview", icon: "overview" },
      { path: "users", label: "User access", icon: "users" },
      { path: "permissions", label: "Role permissions", icon: "permissions" },
      { path: "ai-requests", label: "AI guardrails", icon: "guardrails" },
      { path: "audit-logs", label: "Audit trail", icon: "audit" },
    ],
    patient: [
      { path: "overview", label: "Overview", icon: "overview" },
      { path: "records", label: "My Medical Records", icon: "records" },
      { path: "assistant", label: "Ask AI Assistant", icon: "assistant" },
      // Appointments stay separate from every other request type.
      ...(flags.appointments ? [{ path: "appointments", label: "Appointments", icon: "calendar" }] : []),
      // One page for service requests and medical-record corrections. It replaces the separate
      // "Service Requests" and "AI Request History" items (their old URLs redirect here).
      { path: "requests", label: "Requests", icon: "history" },
      { path: "profile", label: "My Profile", icon: "profile" },
    ],
    technician: [
      { path: "overview", label: "Overview", icon: "overview" },
      { path: "patients", label: "Assigned Patients", icon: "users" },
      ...(flags.serviceRequests ? [{ path: "tasks", label: "Tasks", icon: "pending" }] : []),
      ...(flags.serviceRequests && flags.appointments
        ? [{ path: "scheduling", label: "Scheduling Requests", icon: "calendar" }]
        : []),
    ],
  };
}

export const workspacePages = buildWorkspacePages(readFeatureFlags());

export function dashboardPath(role, page = "") {
  return page ? `/dashboard/${role}/${page}` : `/dashboard/${role}`;
}

/**
 * Decides what a protected route renders for the current auth state.
 * Frontend routing is a convenience; the backend enforces every permission.
 */
export function decideRouteAccess(auth, requiredRole) {
  if (!auth || auth.status === "loading") return { type: "loading" };
  if (auth.status === "configuration-error" || auth.status === "blocked") {
    return { type: "issue" };
  }
  if (auth.status !== "ready" || !auth.user) return { type: "redirect", to: "/login" };
  if (requiredRole && auth.user.role !== requiredRole) {
    return { type: "redirect", to: dashboardPath(auth.user.role) };
  }
  return { type: "allow" };
}

/** Finds the sidebar page for a pathname such as /dashboard/patient/history. */
export function pageForPath(role, pathname) {
  const prefix = `${dashboardPath(role)}/`;
  if (!pathname.startsWith(prefix)) return null;
  const segment = pathname.slice(prefix.length).split("/")[0];
  return workspacePages[role]?.find((page) => page.path === segment) ?? null;
}
