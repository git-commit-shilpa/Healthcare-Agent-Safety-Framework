import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useNavigate,
  useParams,
} from "react-router-dom";
import { supabase } from "../shared/supabase.js";
import PatientDashboard from "../modules/patient/PatientDashboard.jsx";
import {
  ADMIN_IDLE_TIMEOUT_MS,
  createAdminIdleTimer,
} from "../shared/adminIdleTimer.js";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || "/api";
const roles = ["admin", "patient", "physician", "technician"];
const adminSessionExpiredMessage = "Admin session expired. Sign in again.";
const adminSessionExpiredEvent = "admin-session-expired";
const authContext = createContext(null);
const pendingGetRequests = new Map();

async function readApiResponse(response, { notifyAdminExpiry = true } = {}) {
  const result = await response.json();
  if (!response.ok) {
    if (
      notifyAdminExpiry &&
      response.status === 401 &&
      result.detail === adminSessionExpiredMessage
    ) {
      window.dispatchEvent(new Event(adminSessionExpiredEvent));
    }
    throw new Error(result.detail || `Request failed with status ${response.status}.`);
  }
  return result;
}

function getApiJson(path, accessToken) {
  const requestKey = `${path}:${accessToken}`;
  const existingRequest = pendingGetRequests.get(requestKey);
  if (existingRequest) return existingRequest;

  const request = fetch(`${apiBaseUrl}${path}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  })
    .then(readApiResponse);

  let sharedRequest;
  sharedRequest = request.finally(() => {
    if (pendingGetRequests.get(requestKey) === sharedRequest) {
      pendingGetRequests.delete(requestKey);
    }
  });
  pendingGetRequests.set(requestKey, sharedRequest);
  return sharedRequest;
}

function AuthProvider({ children }) {
  const [auth, setAuth] = useState({
    status: "loading",
    session: null,
    user: null,
    adminOverview: null,
    error: "",
  });
  const authRef = useRef(auth);
  const logoutRef = useRef(null);
  authRef.current = auth;

  useEffect(() => {
    if (!supabase) {
      setAuth({
        status: "configuration-error",
        session: null,
        user: null,
        adminOverview: null,
        error: "Supabase Auth is not configured. Set the frontend environment variables and restart Vite.",
      });
      return undefined;
    }

    let active = true;
    let requestNumber = 0;

    async function loadAccount(session) {
      const currentRequest = ++requestNumber;
      if (!active) return;

      if (!session) {
        setAuth({
          status: "signed-out",
          session: null,
          user: null,
          adminOverview: null,
          error: "",
        });
        return;
      }

      const currentAuth = authRef.current;
      if (
        currentAuth.status === "ready" &&
        currentAuth.session?.user.id === session.user.id
      ) {
        setAuth({ ...currentAuth, session });
        return;
      }

      setAuth({
        status: "loading",
        session,
        user: null,
        adminOverview: null,
        error: "",
      });

      try {
        const result = await getApiJson("/auth/bootstrap", session.access_token);
        if (currentRequest === requestNumber && active) {
          setAuth({
            status: "ready",
            session,
            user: result.user,
            adminOverview: result.admin_overview,
            error: "",
          });
        }
      } catch (error) {
        if (currentRequest === requestNumber && active) {
          setAuth({
            status: "blocked",
            session,
            user: null,
            adminOverview: null,
            error:
              error instanceof Error
                ? error.message
                : "Could not validate this account.",
          });
        }
      }
    }

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      void loadAccount(session);
    });

    function handleAdminSessionExpired() {
      setAuth({
        status: "signed-out",
        session: null,
        user: null,
        adminOverview: null,
        error: "",
      });
      void supabase.auth.signOut({ scope: "local" }).then(
        ({ error }) => {
          if (error) console.error("Could not clear the expired local session.", error);
        },
        (error) => console.error("Could not clear the expired local session.", error),
      );
    }

    window.addEventListener(adminSessionExpiredEvent, handleAdminSessionExpired);

    return () => {
      active = false;
      window.removeEventListener(adminSessionExpiredEvent, handleAdminSessionExpired);
      subscription.unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (
      auth.status !== "ready" ||
      auth.user?.role !== "admin" ||
      !auth.session
    ) {
      return undefined;
    }

    let active = true;
    let lastActivitySignalAt = Number.NEGATIVE_INFINITY;
    let queuedActivitySignal = null;
    const timer = createAdminIdleTimer(() => {
      const endSession = logoutRef.current;
      if (endSession) {
        const accessToken = authRef.current.session?.access_token;
        if (accessToken) {
          void fetch(`${apiBaseUrl}/admin/session/logout`, {
            method: "POST",
            headers: { Authorization: `Bearer ${accessToken}` },
          })
            .then((response) =>
              readApiResponse(response, { notifyAdminExpiry: false }),
            )
            .catch((error) => {
              console.error("Could not end the idle Admin session on the backend.", error);
            });
        }
        void endSession({ bestEffort: true }).catch((error) => {
          console.error("Could not clear the idle Admin session.", error);
          setAuth({
            status: "signed-out",
            session: null,
            user: null,
            adminOverview: null,
            error: "",
          });
        });
      }
    });

    async function recordActivity() {
      const accessToken = authRef.current.session?.access_token;
      if (!accessToken) return;

      try {
        const response = await fetch(`${apiBaseUrl}/admin/session/activity`, {
          method: "POST",
          headers: { Authorization: "Bearer " + accessToken },
        });
        await readApiResponse(response);
      } catch (error) {
        if (active) console.error("Could not record Admin activity.", error);
      }
    }

    function handleInteraction(event) {
      if (!event.isTrusted || document.visibilityState !== "visible") return;
      if (!timer.reset()) return;

      const now = Date.now();
      if (now - lastActivitySignalAt >= 1000) {
        if (queuedActivitySignal !== null) {
          clearTimeout(queuedActivitySignal);
          queuedActivitySignal = null;
        }
        lastActivitySignalAt = now;
        void recordActivity();
      } else {
        if (queuedActivitySignal !== null) clearTimeout(queuedActivitySignal);
        queuedActivitySignal = setTimeout(() => {
          queuedActivitySignal = null;
          if (active && document.visibilityState === "visible") {
            lastActivitySignalAt = Date.now();
            void recordActivity();
          }
        }, 1000 - (now - lastActivitySignalAt));
      }
    }

    function handleVisibilityChange() {
      if (document.visibilityState === "visible") timer.check();
    }

    const activityEvents = ["pointerdown", "keydown", "touchstart", "wheel"];
    for (const eventName of activityEvents) {
      document.addEventListener(eventName, handleInteraction, { passive: true });
    }
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      active = false;
      timer.dispose();
      if (queuedActivitySignal !== null) clearTimeout(queuedActivitySignal);
      for (const eventName of activityEvents) {
        document.removeEventListener(eventName, handleInteraction);
      }
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [auth.status, auth.user?.role, auth.session?.user.id]);

  async function login(email, password) {
    if (!supabase) {
      throw new Error("Supabase Auth is not configured.");
    }
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    if (error) throw error;
  }

  async function logout({ bestEffort = false } = {}) {
    if (!supabase) return;
    const currentAuth = authRef.current;
    const isAdmin = currentAuth.user?.role === "admin";
    let backendLogoutError = null;
    if (isAdmin && currentAuth.session?.access_token && !bestEffort) {
      try {
        const response = await fetch(`${apiBaseUrl}/admin/session/logout`, {
          method: "POST",
          headers: { Authorization: "Bearer " + currentAuth.session.access_token },
        });
        await readApiResponse(response, { notifyAdminExpiry: false });
      } catch (error) {
        backendLogoutError = error;
      }
    }

    const { error } = isAdmin
      ? await supabase.auth.signOut({ scope: "local" })
      : await supabase.auth.signOut();
    if (error) throw error;
    if (backendLogoutError && !bestEffort) throw backendLogoutError;
  }
  logoutRef.current = logout;

  return (
    <authContext.Provider value={{ ...auth, login, logout }}>
      {children}
    </authContext.Provider>
  );
}

function useAuth() {
  return useContext(authContext);
}

function LoadingPage({ label = "Restoring your secure session…" }) {
  return (
    <main className="auth-page">
      <div className="auth-card" role="status">
        <span className="brand-name">Cognitive<span>Hospital</span></span>
        <p className="eyebrow">HEALTHCARE AGENT SAFETY FRAMEWORK</p>
        <p className="auth-loading">{label}</p>
      </div>
    </main>
  );
}

function AuthIssuePage() {
  const { status, error, logout } = useAuth();
  const navigate = useNavigate();
  const [signOutError, setSignOutError] = useState("");
  const canSignOut = status === "blocked";

  async function handleSignOut() {
    setSignOutError("");
    try {
      await logout();
      navigate("/login", { replace: true });
    } catch (logoutError) {
      setSignOutError(
        logoutError instanceof Error
          ? logoutError.message
          : "Could not sign out.",
      );
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-card" aria-labelledby="auth-issue-title">
        <a className="brand auth-card-brand" href="/" aria-label="Cognitive Hospital home">
          <span className="brand-name">Cognitive<span>Hospital</span></span>
        </a>
        <p className="eyebrow">ACCOUNT ACCESS</p>
        <h1 id="auth-issue-title">We couldn’t verify access.</h1>
        <p className="auth-description" role="alert">{error}</p>
        {signOutError && <p className="auth-error" role="alert">{signOutError}</p>}
        {canSignOut && (
          <button className="primary-button auth-submit" onClick={handleSignOut} type="button">
            Sign out
          </button>
        )}
      </section>
    </main>
  );
}

function LoginPage() {
  const { status, user, login, error: authError } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (status === "loading") return <LoadingPage />;
  if (status === "configuration-error" || status === "blocked") return <AuthIssuePage />;
  if (status === "ready" && user) {
    return <Navigate to={`/dashboard/${user.role}`} replace />;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await login(email.trim(), password);
    } catch (loginError) {
      setError(
        loginError instanceof Error
          ? loginError.message
          : "Login failed. Check your credentials and try again.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-card" aria-labelledby="login-title">
        <a className="brand auth-card-brand" href="/" aria-label="Cognitive Hospital home">
          <span className="brand-name">Cognitive<span>Hospital</span></span>
        </a>
        <p className="eyebrow">SECURE PROJECT WORKSPACE</p>
        <h1 id="login-title">Welcome back.</h1>
        <p className="auth-description">
          Sign in with the account assigned to you by your project administrator.
        </p>
        {authError && <p className="auth-error" role="alert">{authError}</p>}
        {error && <p className="auth-error" role="alert">{error}</p>}
        <form className="login-form" onSubmit={handleSubmit}>
          <label htmlFor="email">Email address</label>
          <input
            autoComplete="username"
            id="email"
            name="email"
            onChange={(event) => setEmail(event.target.value)}
            required
            type="email"
            value={email}
          />
          <label htmlFor="password">Password</label>
          <input
            autoComplete="current-password"
            id="password"
            name="password"
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
          <button className="primary-button auth-submit" disabled={submitting} type="submit">
            {submitting ? "Signing in…" : "Sign in"}
            <span aria-hidden="true">→</span>
          </button>
        </form>
        <p className="auth-footnote">
          Need an account? Ask your project administrator to invite you and link
          your existing application record.
        </p>
      </section>
    </main>
  );
}

function SetPasswordPage() {
  const { status, session, user } = useAuth();
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  if (status === "loading") return <LoadingPage label="Verifying your invitation…" />;
  if (status === "configuration-error" || status === "blocked") return <AuthIssuePage />;
  if (status !== "ready" || !session) return <Navigate to="/login" replace />;

  async function handleSubmit(event) {
    event.preventDefault();
    setError("");
    if (password !== confirmation) {
      setError("The passwords do not match.");
      return;
    }
    if (!supabase) {
      setError("Supabase Auth is not configured.");
      return;
    }

    setSaving(true);
    try {
      const { error: updateError } = await supabase.auth.updateUser({ password });
      if (updateError) throw updateError;
      navigate(`/dashboard/${user.role}`, { replace: true });
    } catch (updateError) {
      setError(
        updateError instanceof Error
          ? updateError.message
          : "Could not set the password. Use the invitation link again or contact an administrator.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="auth-page">
      <section className="auth-card" aria-labelledby="set-password-title">
        <a className="brand auth-card-brand" href="/" aria-label="Cognitive Hospital home">
          <span className="brand-name">Cognitive<span>Hospital</span></span>
        </a>
        <p className="eyebrow">COMPLETE YOUR ACCOUNT</p>
        <h1 id="set-password-title">Set your password.</h1>
        <p className="auth-description">
          Choose a password for your invited account. Use at least 12 characters.
        </p>
        {error && <p className="auth-error" role="alert">{error}</p>}
        <form className="login-form" onSubmit={handleSubmit}>
          <label htmlFor="new-password">New password</label>
          <input
            autoComplete="new-password"
            id="new-password"
            minLength={12}
            onChange={(event) => setPassword(event.target.value)}
            required
            type="password"
            value={password}
          />
          <label htmlFor="confirm-password">Confirm password</label>
          <input
            autoComplete="new-password"
            id="confirm-password"
            minLength={12}
            onChange={(event) => setConfirmation(event.target.value)}
            required
            type="password"
            value={confirmation}
          />
          <button className="primary-button auth-submit" disabled={saving} type="submit">
            {saving ? "Saving password…" : "Set password"}
          </button>
        </form>
      </section>
    </main>
  );
}

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

function PanelState({ state, onRetry }) {
  if (state.loading) return <p className="panel-message">Loading securely…</p>;
  if (state.error) {
    return (
      <div className="panel-error">
        <p role="alert">{state.error}</p>
        <button className="text-button" onClick={onRetry} type="button">
          Try again
        </button>
      </div>
    );
  }
  if (!state.data?.length) return <p className="panel-message">No records to display.</p>;
  return null;
}

function RolePermissionMatrix({ rows }) {
  const roleNames = ["Admin", "Patient", "Physician", "Technician"];
  const actions = [...new Set(rows.map((row) => row.action))].sort();
  const permissionMap = new Map(
    rows.map((row) => [`${row.role}:${row.action}`, row.allowed]),
  );

  return (
    <div className="admin-table-wrap">
      <table className="admin-table permission-table">
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
                      className={`permission-state ${allowed ? "permission-allowed" : "permission-denied"}`}
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

function AdminStatCard({ label, value, footnote, tone }) {
  const iconNames = {
    users: "users",
    requests: "guardrails",
    escalated: "review",
    audit: "audit",
  };

  return (
    <article className={`admin-stat-card admin-stat-card-${tone}`}>
      <div className="admin-stat-card-top">
        <span className="stat-label">{label}</span>
        <span className={`metric-icon metric-icon-${tone}`} aria-hidden="true">
          <AdminIcon name={iconNames[tone]} />
        </span>
      </div>
      <div className="admin-stat-card-value">
        <strong>{value}</strong>
        <span className="stat-footnote">{footnote}</span>
      </div>
    </article>
  );
}

function AdminIcon({ name }) {
  const iconContent = {
    overview: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /></>,
    users: <><path d="M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20" /><circle cx="10" cy="7.5" r="3.5" /><path d="M17 4.4a3.5 3.5 0 0 1 0 6.7M20 20v-1.5a3.5 3.5 0 0 0-2.5-3.35" /></>,
    permissions: <><path d="M12 3 19 6v5c0 4.6-2.8 8.1-7 10-4.2-1.9-7-5.4-7-10V6l7-3Z" /><path d="m9 12 2 2 4-4" /></>,
    guardrails: <><path d="M12 3 19 6v5c0 4.6-2.8 8.1-7 10-4.2-1.9-7-5.4-7-10V6l7-3Z" /><path d="M12 8v4m0 3h.01" /></>,
    audit: <><path d="M4 12h3l2-5 4 10 2-5h5" /><circle cx="12" cy="12" r="9" /></>,
    review: <><path d="M12 3 19 6v5c0 4.6-2.8 8.1-7 10-4.2-1.9-7-5.4-7-10V6l7-3Z" /><path d="M12 8v4m0 3h.01" /></>,
  };

  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      {iconContent[name]}
    </svg>
  );
}

function AdminDashboard({ user, session, onLogout, initialOverview }) {
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const [activeAdminSection, setActiveAdminSection] = useState("overview");
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

  useEffect(() => {
    const sectionIds = ["overview", "users", "permissions", "ai-requests", "audit-logs"];
    let animationFrame = 0;

    const updateActiveSection = () => {
      animationFrame = 0;
      const visibleSections = sectionIds
        .map((sectionId) => ({
          id: sectionId,
          bounds: document.getElementById(sectionId)?.getBoundingClientRect(),
        }))
        .filter(({ bounds }) => bounds && bounds.bottom > 0 && bounds.top < window.innerHeight);
      const currentSection =
        visibleSections.find(({ bounds }) => bounds.top <= 180 && bounds.bottom > 180)?.id ??
        visibleSections.sort(
          (left, right) =>
            Math.abs(left.bounds.top - 180) - Math.abs(right.bounds.top - 180),
        )[0]?.id ??
        "overview";
      setActiveAdminSection(currentSection);
    };

    const onScroll = () => {
      if (!animationFrame) {
        animationFrame = window.requestAnimationFrame(updateActiveSection);
      }
    };

    updateActiveSection();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("hashchange", onScroll);
    window.addEventListener("resize", onScroll);
    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("hashchange", onScroll);
      window.removeEventListener("resize", onScroll);
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
    };
  }, []);

  const decisions = panels.requests.data.reduce(
    (counts, request) => ({
      ...counts,
      [request.guardrail_decision]: (counts[request.guardrail_decision] || 0) + 1,
    }),
    { Allow: 0, Block: 0, Escalate: 0 },
  );

  return (
    <main className="admin-shell">
      <aside className="admin-sidebar">
        <a className="brand admin-brand" href="/" aria-label="Cognitive Hospital home">
          <span className="brand-name">Cognitive<span>Hospital</span></span>
        </a>
        <p className="application-title">
          Safeguarding Autonomous Agentic AI in Clinical Workflows Through
          Pre-Execution Action Authorization
        </p>
        <p className="sidebar-label">CLINICAL SAFETY WORKSPACE</p>
        <nav className="admin-navigation" aria-label="Admin dashboard sections">
          <a className={`sidebar-link${activeAdminSection === "overview" ? " sidebar-link-active" : ""}`} href="#overview" aria-current={activeAdminSection === "overview" ? "location" : undefined}><AdminIcon name="overview" /><span>Overview</span></a>
          <a className={`sidebar-link${activeAdminSection === "users" ? " sidebar-link-active" : ""}`} href="#users" aria-current={activeAdminSection === "users" ? "location" : undefined}><AdminIcon name="users" /><span>User access</span></a>
          <a className={`sidebar-link${activeAdminSection === "permissions" ? " sidebar-link-active" : ""}`} href="#permissions" aria-current={activeAdminSection === "permissions" ? "location" : undefined}><AdminIcon name="permissions" /><span>Role permissions</span></a>
          <a className={`sidebar-link${activeAdminSection === "ai-requests" ? " sidebar-link-active" : ""}`} href="#ai-requests" aria-current={activeAdminSection === "ai-requests" ? "location" : undefined}><AdminIcon name="guardrails" /><span>AI guardrails</span></a>
          <a className={`sidebar-link${activeAdminSection === "audit-logs" ? " sidebar-link-active" : ""}`} href="#audit-logs" aria-current={activeAdminSection === "audit-logs" ? "location" : undefined}><AdminIcon name="audit" /><span>Audit trail</span></a>
        </nav>
        <div className="sidebar-account">
          <span className="sidebar-avatar" aria-hidden="true">A</span>
          <span className="sidebar-account-copy">
            <strong>Administrator</strong>
            <span>{user.email}</span>
          </span>
        </div>
      </aside>

      <section className="admin-main">
        <header className="admin-topbar">
          <div>
            <span className="breadcrumb">Workspace / <strong>Admin</strong></span>
          </div>
          <div className="account-actions">
            <span className="role-badge">Admin</span>
            <button className="text-button" onClick={onLogout} type="button">Sign out</button>
          </div>
        </header>

        <div className="admin-content">
          <section className="admin-welcome" id="overview">
            <div className="admin-welcome-copy">
              <p className="eyebrow">CLINICAL WORKFLOW SAFETY</p>
              <h1>Pre-execution safety overview</h1>
              <p className="application-full-name">
                Safeguarding Autonomous Agentic AI in Clinical Workflows Through
                Pre-Execution Action Authorization
              </p>
              <p>Review access, AI guardrail decisions, and audit activity in one privacy-conscious workspace.</p>
              <button className="refresh-button" onClick={() => void refreshPanels()} type="button">
                <span aria-hidden="true">↻</span> Refresh data
              </button>
            </div>
            <div className="hero-safety-art" aria-hidden="true">
              <span className="hero-orbit hero-orbit-outer" />
              <span className="hero-orbit hero-orbit-inner" />
              <span className="hero-art-diamond" />
              <svg viewBox="0 0 240 220" fill="none">
                <path className="hero-network-line" d="M39 55 82 82m76-31-39 31m55 67-40-27m-83 34 44-34" />
                <path className="hero-shield" d="M120 38 166 57v36c0 31-18 55-46 69-28-14-46-38-46-69V57l46-19Z" />
                <path className="hero-shield-check" d="m101 96 13 13 27-29" />
                <circle className="hero-node hero-node-teal" cx="38" cy="52" r="8" />
                <circle className="hero-node hero-node-coral" cx="164" cy="49" r="9" />
                <circle className="hero-node hero-node-blue" cx="193" cy="159" r="7" />
                <circle className="hero-node hero-node-lavender" cx="57" cy="166" r="8" />
              </svg>
              <span className="hero-art-spark hero-art-spark-one">✦</span>
              <span className="hero-art-spark hero-art-spark-two">✧</span>
            </div>
          </section>

          <div className="admin-stat-grid" aria-label="Recent activity summary">
            <AdminStatCard
              label="APPLICATION USERS"
              value={panels.users.loading ? "—" : panels.users.data.length}
              footnote="Accounts in this page"
              tone="users"
            />
            <AdminStatCard
              label="AI REQUESTS"
              value={panels.requests.loading ? "—" : panels.requests.data.length}
              footnote="Most recent requests"
              tone="requests"
            />
            <AdminStatCard
              label="ESCALATED"
              value={panels.requests.loading ? "—" : decisions.Escalate}
              footnote="In the displayed requests"
              tone="escalated"
            />
            <AdminStatCard
              label="AUDIT EVENTS"
              value={panels.audit.loading ? "—" : panels.audit.data.length}
              footnote="Most recent events"
              tone="audit"
            />
          </div>

          <section className="admin-panel admin-panel-users" id="users">
            <div className="admin-panel-heading">
              <div className="admin-panel-heading-main">
                <span className="section-icon section-icon-users" aria-hidden="true"><AdminIcon name="users" /></span>
                <div>
                  <p className="eyebrow">ACCOUNT DIRECTORY</p>
                  <h2>User management</h2>
                  <p className="panel-subtitle">Invite users and deactivate access while preserving account history.</p>
                </div>
              </div>
              <span className="readonly-badge">History preserved</span>
            </div>
            <form className="invite-form" onSubmit={handleInvite}>
              <label>
                Name
                <input
                  autoComplete="name"
                  maxLength={120}
                  onChange={(event) => setInviteForm((current) => ({ ...current, name: event.target.value }))}
                  required
                  value={inviteForm.name}
                />
              </label>
              <label>
                Email
                <input
                  autoComplete="email"
                  onChange={(event) => setInviteForm((current) => ({ ...current, email: event.target.value }))}
                  required
                  type="email"
                  value={inviteForm.email}
                />
              </label>
              <label>
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
              <button className="primary-button invite-submit" disabled={inviting} type="submit">
                {inviting ? "Sending invitation…" : "Invite user"}
              </button>
            </form>
            {inviteStatus && <p className="invite-success" role="status">{inviteStatus}</p>}
            {inviteError && <p className="auth-error" role="alert">{inviteError}</p>}
            {deactivationStatus && <p className="invite-success" role="status">{deactivationStatus}</p>}
            {deactivationError && <p className="auth-error" role="alert">{deactivationError}</p>}
            <PanelState state={panels.users} onRetry={() => void refreshPanels()} />
            {!panels.users.loading && !panels.users.error && panels.users.data.length > 0 && (
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead><tr><th scope="col">Name</th><th scope="col">Email</th><th scope="col">Role</th><th scope="col">Status</th><th scope="col">Account access</th></tr></thead>
                  <tbody>
                    {panels.users.data.map((record) => (
                      <tr key={record.user_id}>
                        <td>{record.name}</td>
                        <td>{record.email}</td>
                        <td><span className="table-role">{record.role}</span></td>
                        <td>
                          <span className={`user-status ${record.is_active ? "user-status-active" : "user-status-inactive"}`}>
                            {record.is_active ? "Active" : "Deactivated"}
                          </span>
                        </td>
                        <td>
                          {record.user_id === user.user_id ? (
                            <span className="readonly-badge">Current account</span>
                          ) : record.is_active ? (
                            <button
                              className="deactivate-button"
                              disabled={deactivatingUserId !== null}
                              onClick={() => void handleDeactivate(record)}
                              type="button"
                            >
                              {deactivatingUserId === record.user_id ? "Deactivating…" : "Deactivate"}
                            </button>
                          ) : (
                            <span className="panel-message">Access removed</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="admin-panel admin-panel-permissions" id="permissions">
            <div className="admin-panel-heading">
              <div className="admin-panel-heading-main">
                <span className="section-icon section-icon-permissions" aria-hidden="true"><AdminIcon name="permissions" /></span>
                <div>
                  <p className="eyebrow">ACCESS CONTROL</p>
                  <h2>Role permissions</h2>
                  <p className="panel-subtitle">Current permissions from the database. This matrix cannot be edited here.</p>
                </div>
              </div>
              <span className="readonly-badge">Read only</span>
            </div>
            <PanelState state={panels.permissions} onRetry={() => void refreshPanels()} />
            {!panels.permissions.loading && !panels.permissions.error && panels.permissions.data.length > 0 && (
              <RolePermissionMatrix rows={panels.permissions.data} />
            )}
            <p className="privacy-note">
              Admin is not permitted to view patient medical records. Role permissions do not grant unrestricted patient-data access.
            </p>
          </section>

          <section className="admin-panel admin-panel-ai" id="ai-requests">
            <div className="admin-panel-heading">
              <div className="admin-panel-heading-main">
                <span className="section-icon section-icon-ai" aria-hidden="true"><AdminIcon name="guardrails" /></span>
                <div>
                  <p className="eyebrow">GUARDRAIL ACTIVITY</p>
                  <h2>AI request monitoring</h2>
                  <p className="panel-subtitle">Request metadata and decisions only; clinical request content is excluded.</p>
                </div>
              </div>
              <span className="readonly-badge">Read only</span>
            </div>
            <div className="decision-legend">
              <span><i className="decision-allow" /> Allow</span>
              <span><i className="decision-block" /> Block</span>
              <span><i className="decision-escalate" /> Escalate</span>
            </div>
            <form className="ai-analysis" onSubmit={handleAnalyzeRequests}>
              <label htmlFor="ai-request-question">Ask AI about request decisions</label>
              <div className="ai-analysis-controls">
                <input
                  id="ai-request-question"
                  maxLength={500}
                  onChange={(event) => setAnalysisQuestion(event.target.value)}
                  placeholder="e.g. Summarize the blocked requests"
                  required
                  value={analysisQuestion}
                />
                <button
                  className="primary-button ai-analysis-submit"
                  disabled={analyzingRequests || analysisQuestion.trim().length < 3}
                  type="submit"
                >
                  {analyzingRequests ? "Analyzing…" : "Ask AI"}
                </button>
              </div>
              <p className="ai-analysis-note">
                Uses the latest 100 request IDs, actions, decisions, and timestamps only. Request content and block rationales are not provided to AI.
              </p>
            </form>
            {analysisError && <p className="auth-error" role="alert">{analysisError}</p>}
            {analysisAnswer && (
              <div className="ai-analysis-answer" role="status">
                <p>{analysisAnswer}</p>
                <span>Based on {analysisRecordCount} recent request record(s).</span>
              </div>
            )}
            <PanelState state={panels.requests} onRetry={() => void refreshPanels()} />
            {!panels.requests.loading && !panels.requests.error && panels.requests.data.length > 0 && (
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead><tr><th scope="col">Request</th><th scope="col">Action</th><th scope="col">Guardrail decision</th><th scope="col">Created</th></tr></thead>
                  <tbody>
                    {panels.requests.data.map((request) => (
                      <tr key={request.request_id}>
                        <td>#{request.request_id}</td>
                        <td>{request.action}</td>
                        <td><span className={`decision-badge decision-${request.guardrail_decision.toLowerCase()}`}>{request.guardrail_decision}</span></td>
                        <td>{request.created_at ? new Date(request.created_at).toLocaleString() : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="admin-panel admin-panel-audit" id="audit-logs">
            <div className="admin-panel-heading">
              <div className="admin-panel-heading-main">
                <span className="section-icon section-icon-audit" aria-hidden="true"><AdminIcon name="audit" /></span>
                <div>
                  <p className="eyebrow">SECURITY</p>
                  <h2>Audit log</h2>
                  <p className="panel-subtitle">Append-only event metadata. Free-text details are intentionally withheld.</p>
                </div>
              </div>
              <span className="readonly-badge">Read only</span>
            </div>
            <form className="ai-analysis audit-ai-analysis" onSubmit={handleAnalyzeAuditLogs}>
              <label htmlFor="audit-log-question">Ask AI about Audit Logs</label>
              <div className="ai-analysis-controls">
                <input
                  id="audit-log-question"
                  maxLength={500}
                  onChange={(event) => setAuditAnalysisQuestion(event.target.value)}
                  placeholder="e.g. Count recent events by type and summarize what was recorded"
                  required
                  value={auditAnalysisQuestion}
                />
                <button
                  className="primary-button ai-analysis-submit"
                  disabled={analyzingAuditLogs || auditAnalysisQuestion.trim().length < 3}
                  type="submit"
                >
                  {analyzingAuditLogs ? "Analyzing…" : "Ask AI"}
                </button>
              </div>
              <p className="ai-analysis-note">
                Analyzes authorized event metadata only. User identities, free-text details, and patient information are excluded.
              </p>
            </form>
            {analyzingAuditLogs && (
              <p className="audit-analysis-loading" role="status" aria-live="polite">
                Analyzing audit metadata with the AI model…
              </p>
            )}
            {auditAnalysisError && <p className="auth-error audit-analysis-error" role="alert">{auditAnalysisError}</p>}
            {auditAnalysisResult && (
              <div className="audit-analysis-result" role="region" aria-label="Audit analysis results">
                {auditAnalysisResult.records_analyzed === 0 ? (
                  <p className="audit-analysis-empty" role="status">
                    No audit records were available for this analysis.
                  </p>
                ) : (
                  <>
                    <section className="verified-event-counts" aria-labelledby="verified-event-counts-title">
                      <div className="audit-result-heading">
                        <h3 id="verified-event-counts-title">Verified event counts</h3>
                        <span>{auditAnalysisResult.records_analyzed} records · limit {auditAnalysisResult.record_limit}</span>
                      </div>
                      {Object.keys(auditAnalysisResult.event_type_counts || {}).length > 0 ? (
                        <ul className="event-count-list">
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
                        <p className="audit-analysis-empty">No event categories were returned.</p>
                      )}
                    </section>
                    <section className="audit-ai-explanation" aria-labelledby="audit-ai-explanation-title">
                      <h3 id="audit-ai-explanation-title">AI explanation</h3>
                      <p>{auditAnalysisResult.answer}</p>
                    </section>
                  </>
                )}
              </div>
            )}
            <PanelState state={panels.audit} onRetry={() => void refreshPanels()} />
            {!panels.audit.loading && !panels.audit.error && panels.audit.data.length > 0 && (
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead><tr><th scope="col">Event</th><th scope="col">Type</th><th scope="col">Request</th><th scope="col">Created</th></tr></thead>
                  <tbody>
                    {panels.audit.data.map((event) => (
                      <tr key={event.log_id}>
                        <td>#{event.log_id}</td>
                        <td>{event.event_type}</td>
                        <td>{event.request_id ? `#${event.request_id}` : "—"}</td>
                        <td>{event.created_at ? new Date(event.created_at).toLocaleString() : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <footer className="admin-footer">Cognitive Hospital · Privacy-first administration</footer>
        </div>
      </section>
    </main>
  );
}

function RoleDashboard() {
  const { role: requestedRole } = useParams();
  const { status, session, user, adminOverview, logout } = useAuth();
  const [dashboardMessage, setDashboardMessage] = useState("");
  const [dashboardError, setDashboardError] = useState("");
  const [signOutError, setSignOutError] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    if (
      status !== "ready" ||
      !session ||
      requestedRole === "admin" ||
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

  if (status === "loading") return <LoadingPage />;
  if (status === "configuration-error" || status === "blocked") return <AuthIssuePage />;
  if (status !== "ready" || !user) return <Navigate to="/login" replace />;
  if (user.role !== requestedRole) {
    return <Navigate to={`/dashboard/${user.role}`} replace />;
  }
  if (!roles.includes(requestedRole)) {
    return <Navigate to={`/dashboard/${user.role}`} replace />;
  }

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

  const displayRole = user.role.charAt(0).toUpperCase() + user.role.slice(1);

  if (user.role === "admin") {
    return (
      <AdminDashboard
        user={user}
        session={session}
        onLogout={handleLogout}
        initialOverview={adminOverview}
      />
    );
  }

  if (user.role === "patient") {
    return (
      <PatientDashboard
        user={user}
        session={session}
        dashboardError={dashboardError}
        onLogout={handleLogout}
        signOutError={signOutError}
      />
    );
  }

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

function AppRoutes() {
  return (
    <Routes>
      <Route element={<HomeRedirect />} path="/" />
      <Route element={<LoginPage />} path="/login" />
      <Route element={<SetPasswordPage />} path="/set-password" />
      <Route element={<RoleDashboard />} path="/dashboard/:role" />
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
