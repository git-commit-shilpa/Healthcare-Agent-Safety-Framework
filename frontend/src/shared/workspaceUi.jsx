import { Link } from "react-router-dom";

import { WorkspaceIcon } from "./WorkspaceShell.jsx";

/** Content card used on every workspace page. tone: 1–4 selects a theme accent. */
export function Panel({ tone = 1, icon, eyebrow, title, titleId, subtitle, aside, children, className = "" }) {
  return (
    <section className={`ws-panel ws-tone-${tone} ${className}`.trim()} aria-labelledby={titleId}>
      <div className="ws-panel-heading">
        <div className="ws-panel-heading-main">
          {icon && (
            <span className="ws-panel-icon" aria-hidden="true"><WorkspaceIcon name={icon} /></span>
          )}
          <div>
            {eyebrow && <p className="ws-eyebrow">{eyebrow}</p>}
            <h2 id={titleId}>{title}</h2>
            {subtitle && <p className="ws-panel-subtitle">{subtitle}</p>}
          </div>
        </div>
        {aside}
      </div>
      {children}
    </section>
  );
}

export function Chip({ children }) {
  return <span className="ws-chip">{children}</span>;
}

export function StatCard({ tone, icon, label, value, note, textValue = false }) {
  return (
    <article className={`ws-stat ws-tone-${tone}${textValue ? " ws-stat-text" : ""}`}>
      <div className="ws-stat-top">
        <span className="ws-stat-label">{label}</span>
        <span className="ws-stat-icon" aria-hidden="true"><WorkspaceIcon name={icon} /></span>
      </div>
      <div className="ws-stat-value">
        <strong>{value}</strong>
        <span className="ws-stat-note">{note}</span>
      </div>
    </article>
  );
}

/** Large link card for jumping to another page of the workspace. */
export function ModuleLink({ to, tone, icon, title, description, meta }) {
  return (
    <Link className={`ws-module ws-tone-${tone}`} to={to}>
      <span className="ws-module-icon" aria-hidden="true"><WorkspaceIcon name={icon} /></span>
      <span className="ws-module-copy">
        <strong>{title}</strong>
        <span>{description}</span>
        {meta && <span className="ws-module-meta">{meta}</span>}
      </span>
      <span className="ws-module-arrow" aria-hidden="true"><WorkspaceIcon name="arrow" /></span>
    </Link>
  );
}

export function RefreshButton({ onClick, busy = false, label = "Refresh data" }) {
  return (
    <button className="ws-btn ws-btn-secondary" disabled={busy} onClick={onClick} type="button">
      <WorkspaceIcon name="refresh" />
      {busy ? "Refreshing…" : label}
    </button>
  );
}

export function LoadingMessage({ children = "Loading securely…" }) {
  return (
    <p className="ws-message ws-message-loading" role="status">
      <span className="ws-spinner" aria-hidden="true" />
      {children}
    </p>
  );
}

export function EmptyMessage({ children }) {
  return <p className="ws-message" role="status">{children}</p>;
}

export function ErrorMessage({ message, onRetry, retryLabel = "Try again" }) {
  return (
    <div className="ws-error">
      <p role="alert">{message}</p>
      {onRetry && (
        <button className="ws-link-button" onClick={onRetry} type="button">
          {retryLabel}
        </button>
      )}
    </div>
  );
}

export function Badge({ tone, children }) {
  return <span className={`ws-badge ws-badge-${tone}`}>{children}</span>;
}
