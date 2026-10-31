import { useEffect, useState } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";

import {
  AdminAiRequestsPage,
  AdminAuditLogsPage,
  AdminOverviewPage,
  AdminPermissionsPage,
  AdminUsersPage,
} from "../modules/admin/AdminPages.jsx";
import AdminWorkspace from "../modules/admin/AdminWorkspace.jsx";
import PatientRecordsPage from "../modules/patient/MedicalRecordsSection.jsx";
import PatientAssistantPage from "../modules/patient/PatientAssistantPage.jsx";
import PatientOverviewPage from "../modules/patient/PatientOverview.jsx";
import PatientWorkspace from "../modules/patient/PatientWorkspace.jsx";
import PatientProfilePage from "../modules/patient/ProfileSection.jsx";
import PatientHistoryPage from "../modules/patient/RequestHistorySection.jsx";
import { getApiJson, roles } from "../shared/apiClient.js";
import { AuthProvider, useAuth } from "./auth.jsx";
import { AuthIssuePage, LoadingPage, LoginPage, SetPasswordPage } from "./AuthPages.jsx";
import { decideRouteAccess } from "./routeAccess.js";

/** Renders children only for the required role; otherwise redirects. */
function RequireRole({ role, children }) {
  const auth = useAuth();
  const access = decideRouteAccess(auth, role);
  if (access.type === "loading") return <LoadingPage />;
  if (access.type === "issue") return <AuthIssuePage />;
  if (access.type === "redirect") return <Navigate to={access.to} replace />;
  return children;
}

function useSignOut() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [signOutError, setSignOutError] = useState("");

  async function handleLogout() {
    setSignOutError("");
    try {
      await logout();
      navigate("/login", { replace: true });
    } catch (error) {
      setSignOutError(
        error instanceof Error ? error.message : "Could not sign out.",
      );
    }
  }

  return { handleLogout, signOutError };
}

function AdminLayoutRoute() {
  const { session, user, adminOverview } = useAuth();
  const { handleLogout, signOutError } = useSignOut();
  return (
    <AdminWorkspace
      initialOverview={adminOverview}
      onLogout={handleLogout}
      session={session}
      signOutError={signOutError}
      user={user}
    />
  );
}

function PatientLayoutRoute() {
  const { session, user } = useAuth();
  const { handleLogout, signOutError } = useSignOut();
  return (
    <PatientWorkspace
      onLogout={handleLogout}
      session={session}
      signOutError={signOutError}
      user={user}
    />
  );
}

/** Placeholder workspace for roles whose modules are not built yet. */
function RoleDashboard() {
  const { role: requestedRole } = useParams();
  const { status, session, user } = useAuth();
  const [dashboardMessage, setDashboardMessage] = useState("");
  const [dashboardError, setDashboardError] = useState("");
  const { handleLogout, signOutError } = useSignOut();

  useEffect(() => {
    if (
      status !== "ready" ||
      !session ||
      !roles.includes(requestedRole)
    ) return;

    let active = true;
    setDashboardMessage("");
    setDashboardError("");

    getApiJson(`/dashboard/${requestedRole}`, session.access_token)
      .then((result) => {
        if (active) setDashboardMessage(result.message);
      })
      .catch((requestError) => {
        if (active) {
          setDashboardError(
            requestError instanceof Error
              ? requestError.message
              : "Could not load the dashboard.",
          );
        }
      });

    return () => {
      active = false;
    };
  }, [requestedRole, session?.user.id, status]);

  if (user.role !== requestedRole || !roles.includes(requestedRole)) {
    return <Navigate to={`/dashboard/${user.role}`} replace />;
  }

  const displayRole = user.role.charAt(0).toUpperCase() + user.role.slice(1);

  return (
    <main className="page-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Cognitive Hospital home">
          <span className="brand-name">Cognitive<span>Hospital</span></span>
        </a>
        <div className="account-actions">
          <span className="role-badge">{displayRole}</span>
          <button className="text-button" onClick={handleLogout} type="button">
            Sign out
          </button>
        </div>
      </header>
      <section className="dashboard-card" aria-labelledby="dashboard-title">
        <p className="eyebrow">SECURE PROJECT WORKSPACE</p>
        <h1 id="dashboard-title">{displayRole} dashboard</h1>
        <p className="dashboard-welcome">
          {dashboardMessage || "Verifying your role access…"}
        </p>
        {dashboardError && <p className="auth-error" role="alert">{dashboardError}</p>}
        {signOutError && <p className="auth-error" role="alert">{signOutError}</p>}
        <p className="account-email">{user.email}</p>
      </section>
      <section className="dashboard-placeholder">
        <span className="card-icon icon-shared" aria-hidden="true">+</span>
        <h2>Shared workspace</h2>
        <p>
          Your role-specific features will be added by the project team. This
          sign-in foundation does not display patient records.
        </p>
      </section>
    </main>
  );
}

function HomeRedirect() {
  const { status, user } = useAuth();
  if (status === "loading") return <LoadingPage />;
  if (status === "configuration-error" || status === "blocked") return <AuthIssuePage />;
  return user ? (
    <Navigate to={`/dashboard/${user.role}`} replace />
  ) : (
    <Navigate to="/login" replace />
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<HomeRedirect />} path="/" />
      <Route element={<LoginPage />} path="/login" />
      <Route element={<SetPasswordPage />} path="/set-password" />

      <Route
        element={<RequireRole role="admin"><AdminLayoutRoute /></RequireRole>}
        path="/dashboard/admin"
      >
        <Route element={<Navigate replace to="overview" />} index />
        <Route element={<AdminOverviewPage />} path="overview" />
        <Route element={<AdminUsersPage />} path="users" />
        <Route element={<AdminPermissionsPage />} path="permissions" />
        <Route element={<AdminAiRequestsPage />} path="ai-requests" />
        <Route element={<AdminAuditLogsPage />} path="audit-logs" />
        <Route element={<Navigate replace to="/dashboard/admin/overview" />} path="*" />
      </Route>

      <Route
        element={<RequireRole role="patient"><PatientLayoutRoute /></RequireRole>}
        path="/dashboard/patient"
      >
        <Route element={<Navigate replace to="overview" />} index />
        <Route element={<PatientOverviewPage />} path="overview" />
        <Route element={<PatientRecordsPage />} path="records" />
        <Route element={<PatientAssistantPage />} path="assistant" />
        <Route element={<PatientHistoryPage />} path="history" />
        <Route element={<PatientProfilePage />} path="profile" />
        <Route element={<Navigate replace to="/dashboard/patient/overview" />} path="*" />
      </Route>

      <Route element={<RequireRole><RoleDashboard /></RequireRole>} path="/dashboard/:role" />
      <Route element={<HomeRedirect />} path="*" />
    </Routes>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
