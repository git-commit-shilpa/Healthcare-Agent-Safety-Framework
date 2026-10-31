import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { supabase } from "../shared/supabase.js";
import { useAuth } from "./auth.jsx";
import { AuthLayout } from "./AuthLayout.jsx";

export function LoadingPage({ label = "Restoring your secure session…" }) {
  return (
    <AuthLayout>
      <div className="auth-loading-state" role="status">
        <span className="auth-spinner" aria-hidden="true" />
        <p className="auth-eyebrow">HEALTHCARE AGENT SAFETY FRAMEWORK</p>
        <p className="auth-loading">{label}</p>
      </div>
    </AuthLayout>
  );
}

export function AuthIssuePage() {
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
    <AuthLayout>
      <section aria-labelledby="auth-issue-title">
        <p className="auth-eyebrow">ACCOUNT ACCESS</p>
        <h1 id="auth-issue-title">We couldn’t verify access.</h1>
        <p className="auth-description" role="alert">{error}</p>
        {signOutError && <p className="auth-error" role="alert">{signOutError}</p>}
        {canSignOut && (
          <button className="auth-submit" onClick={handleSignOut} type="button">
            Sign out
          </button>
        )}
      </section>
    </AuthLayout>
  );
}

export function LoginPage() {
  const { status, user, login, error: authError } = useAuth();
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
    <AuthLayout>
      <section aria-labelledby="login-title">
        <p className="auth-eyebrow">SECURE SIGN-IN</p>
        <h1 id="login-title">Welcome back.</h1>
        <p className="auth-description">
          Sign in with the account assigned to you by your project administrator.
          You’ll be taken to the workspace for your role.
        </p>
        {authError && <p className="auth-error" role="alert">{authError}</p>}
        {error && <p className="auth-error" role="alert">{error}</p>}
        <form className="auth-form" onSubmit={handleSubmit}>
          <div className="auth-field">
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
          </div>
          <div className="auth-field">
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
          </div>
          <button className="auth-submit" disabled={submitting} type="submit">
            {submitting ? "Signing in…" : "Sign in"}
            <span aria-hidden="true">→</span>
          </button>
        </form>
        <p className="auth-footnote">
          Need an account? Ask your project administrator to invite you and link
          your existing application record.
        </p>
      </section>
    </AuthLayout>
  );
}

export function SetPasswordPage() {
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
    <AuthLayout>
      <section aria-labelledby="set-password-title">
        <p className="auth-eyebrow">COMPLETE YOUR ACCOUNT</p>
        <h1 id="set-password-title">Set your password.</h1>
        <p className="auth-description">
          Choose a password for your invited account. Use at least 12 characters.
        </p>
        {error && <p className="auth-error" role="alert">{error}</p>}
        <form className="auth-form" onSubmit={handleSubmit}>
          <div className="auth-field">
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
          </div>
          <div className="auth-field">
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
          </div>
          <button className="auth-submit" disabled={saving} type="submit">
            {saving ? "Saving password…" : "Set password"}
          </button>
        </form>
      </section>
    </AuthLayout>
  );
}
