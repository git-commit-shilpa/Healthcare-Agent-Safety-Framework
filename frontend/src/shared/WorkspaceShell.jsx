import { useEffect, useRef } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { dashboardPath, pageForPath, workspacePages } from "../app/routeAccess.js";

const iconContent = {
  overview: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /></>,
  users: <><path d="M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20" /><circle cx="10" cy="7.5" r="3.5" /><path d="M17 4.4a3.5 3.5 0 0 1 0 6.7M20 20v-1.5a3.5 3.5 0 0 0-2.5-3.35" /></>,
  permissions: <><path d="M12 3 19 6v5c0 4.6-2.8 8.1-7 10-4.2-1.9-7-5.4-7-10V6l7-3Z" /><path d="m9 12 2 2 4-4" /></>,
  guardrails: <><path d="M12 3 19 6v5c0 4.6-2.8 8.1-7 10-4.2-1.9-7-5.4-7-10V6l7-3Z" /><path d="M12 8v4m0 3h.01" /></>,
  audit: <><path d="M4 12h3l2-5 4 10 2-5h5" /><circle cx="12" cy="12" r="9" /></>,
  records: <><path d="M7 3.5h7l4 4V20a1 1 0 0 1-1 1H7a2 2 0 0 1-2-2V5.5a2 2 0 0 1 2-2Z" /><path d="M14 3.5v5h5M8.5 13h7m-7 3.5h7" /></>,
  assistant: <><path d="M12 3.5a8.5 8.5 0 0 0-7.3 12.9L4 20l3.8-.8A8.5 8.5 0 1 0 12 3.5Z" /><path d="M8.5 11h.01M12 11h.01M15.5 11h.01" /></>,
  history: <><path d="M4.5 8.5A8 8 0 1 1 4 13" /><path d="M4.5 4.5v4h4m3.5-.5v5l3 2" /></>,
  profile: <><circle cx="12" cy="8" r="3.5" /><path d="M5 20a7 7 0 0 1 14 0" /></>,
  pulse: <><path d="M3.5 12h4l2-5 4 10 2-5h5" /></>,
  pending: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
  escalate: <><path d="M12 3 19 6v5c0 4.6-2.8 8.1-7 10-4.2-1.9-7-5.4-7-10V6l7-3Z" /><path d="M12 8v4m0 3h.01" /></>,
  arrow: <><path d="M5 12h14m-5-5 5 5-5 5" /></>,
  refresh: <><path d="M19.5 8.5A8 8 0 0 0 5 7" /><path d="M4.5 3.5v4h4M4.5 15.5A8 8 0 0 0 19 17" /><path d="M19.5 20.5v-4h-4" /></>,
  signout: <><path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" /><path d="M10 16l-4-4 4-4m-4 4h10" /></>,
};

export function WorkspaceIcon({ name }) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      focusable="false"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
      viewBox="0 0 24 24"
    >
      {iconContent[name]}
    </svg>
  );
}

export function HasfLogo() {
  return (
    <svg aria-hidden="true" className="ws-logo" focusable="false" viewBox="0 0 40 40">
      <defs>
        <linearGradient id="hasf-logo-gradient" x1="0" x2="1" y1="0" y2="1">
          {/* Colours come from the theme via .ws-logo-start / .ws-logo-end in CSS. */}
          <stop className="ws-logo-start" offset="0" stopColor="#22d3ee" />
          <stop className="ws-logo-end" offset="1" stopColor="#7c5cff" />
        </linearGradient>
      </defs>
      <path d="M20 3 34 9v10c0 9-6 15.5-14 18C12 34.5 6 28 6 19V9l14-6Z" fill="url(#hasf-logo-gradient)" />
      <path d="M20 12v14M13 19h14" stroke="#fff" strokeLinecap="round" strokeWidth="3" />
      <circle cx="20" cy="19" fill="#fff" r="2.4" />
    </svg>
  );
}

/** Page heading used at the top of every workspace page except Overview. */
export function PageHeader({ icon, eyebrow, title, description, actions }) {
  return (
    <header className="ws-page-header">
      <span className="ws-page-icon" aria-hidden="true"><WorkspaceIcon name={icon} /></span>
      <div className="ws-page-heading">
        <p className="ws-eyebrow">{eyebrow}</p>
        <h1 id="ws-page-title" tabIndex={-1}>{title}</h1>
        {description && <p className="ws-page-description">{description}</p>}
      </div>
      {actions && <div className="ws-page-actions">{actions}</div>}
    </header>
  );
}

/**
 * Persistent sidebar + header shared by every role workspace. Each sidebar
 * item is a separate route; the main area changes with the URL.
 */
export function WorkspaceShell({
  role,
  theme,
  roleLabel,
  workspaceLabel,
  account,
  onLogout,
  alerts,
  children,
}) {
  const location = useLocation();
  const currentPage = pageForPath(role, location.pathname);
  const firstRender = useRef(true);

  useEffect(() => {
    document.title = `${currentPage?.label ?? roleLabel} · ${roleLabel} · HASF`;
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    // New page: start at the top and move focus to its heading for
    // keyboard and screen-reader users.
    window.scrollTo(0, 0);
    document.getElementById("ws-page-title")?.focus({ preventScroll: true });
  }, [location.pathname, currentPage?.label, roleLabel]);

  return (
    <div className={`ws-shell ws-theme-${theme}`}>
      <a className="ws-skip-link" href="#ws-main">Skip to main content</a>
      <aside className="ws-sidebar">
        <a className="ws-brand" href="/" aria-label="HASF home">
          <HasfLogo />
          <span className="ws-brand-text">
            <strong>HASF</strong>
            <span>Cognitive Hospital</span>
          </span>
        </a>
        <p className="ws-app-title">Healthcare Agent Safety Framework</p>
        <p className="ws-nav-label">{workspaceLabel}</p>
        <nav className="ws-nav" aria-label={`${roleLabel} pages`}>
          {workspacePages[role].map((page) => (
            <NavLink
              className={({ isActive }) => `ws-nav-link${isActive ? " is-active" : ""}`}
              key={page.path}
              to={dashboardPath(role, page.path)}
            >
              <WorkspaceIcon name={page.icon} />
              <span>{page.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="ws-account">
          <span className="ws-avatar" aria-hidden="true">{account.initials}</span>
          <span className="ws-account-copy">
            <strong>{account.name}</strong>
            <span>{account.detail}</span>
          </span>
        </div>
      </aside>

      <div className="ws-main">
        <header className="ws-header">
          <nav className="ws-crumbs" aria-label="Breadcrumb">
            <span>Workspace</span>
            <span aria-hidden="true">/</span>
            <span>{roleLabel}</span>
            {currentPage && (
              <>
                <span aria-hidden="true">/</span>
                <strong aria-current="page">{currentPage.label}</strong>
              </>
            )}
          </nav>
          <div className="ws-header-actions">
            <span className="ws-role-pill">{roleLabel}</span>
            <button className="ws-signout" onClick={onLogout} type="button">
              <WorkspaceIcon name="signout" />
              Sign out
            </button>
          </div>
        </header>

        <main className="ws-content" id="ws-main">
          {alerts}
          <div className="ws-page" key={location.pathname}>
            {children}
          </div>
          <footer className="ws-footer">
            HASF · Role-based access · Guardrail decisions · Human review · Audited actions
          </footer>
        </main>
      </div>
    </div>
  );
}
