import { Link, useOutletContext } from "react-router-dom";

import { ModuleLink, StatCard } from "../../shared/workspaceUi.jsx";
import {
  describeApprovalStatus,
  findBlockingRequest,
  formatDateTime,
} from "./patientCorrection.js";

function recordCard(recordState) {
  if (recordState.status === "ready") {
    return {
      value: recordState.record.medical_condition ?? "On file",
      note: `Treatment: ${recordState.record.treatment ?? "Not provided"}`,
    };
  }
  if (recordState.status === "empty") return { value: "Not linked", note: "No medical record on file" };
  if (recordState.status === "error") return { value: "—", note: "Could not load — open My Medical Records" };
  return { value: "—", note: "Loading your record…" };
}

function historyCards(historyState) {
  if (historyState.status === "forbidden") {
    const unavailable = { value: "—", note: "Not available for this account" };
    return { total: unavailable, pending: unavailable, latest: unavailable };
  }
  if (historyState.status !== "ready") {
    const note = historyState.status === "error" ? "Could not load — open Request History" : "Loading…";
    return { total: { value: "—", note }, pending: { value: "—", note }, latest: { value: "—", note } };
  }

  const { items } = historyState;
  const pendingCount = items.filter((item) => findBlockingRequest([item])).length;
  const latest = findBlockingRequest(items) ?? items[0];
  return {
    total: { value: items.length, note: "Treatment corrections submitted" },
    pending: { value: pendingCount, note: "Awaiting Physician review" },
    latest: latest
      ? {
          value: describeApprovalStatus(latest).label,
          note: `Request #${latest.request_id} · Submitted ${formatDateTime(latest.created_at) ?? "—"}`,
        }
      : { value: "No requests", note: "Nothing submitted yet" },
  };
}

function CareArt() {
  return (
    <div className="ws-hero-art" aria-hidden="true">
      <span className="ws-hero-ring ws-hero-ring-outer" />
      <span className="ws-hero-ring ws-hero-ring-inner" />
      <svg viewBox="0 0 240 220" fill="none">
        <path className="ws-hero-line" d="M39 55 82 82m76-31-39 31m55 67-40-27m-83 34 44-34" />
        <path
          className="ws-hero-glyph"
          d="M120 158c-30-19-50-38-50-63 0-16 12-28 27-28 10 0 18 5 23 13 5-8 13-13 23-13 15 0 27 12 27 28 0 25-20 44-50 63Z"
        />
        <path className="ws-hero-mark" d="M120 89v34m-17-17h34" />
        <circle className="ws-hero-node ws-node-1" cx="38" cy="52" r="8" />
        <circle className="ws-hero-node ws-node-2" cx="164" cy="49" r="9" />
        <circle className="ws-hero-node ws-node-3" cx="193" cy="159" r="7" />
        <circle className="ws-hero-node ws-node-4" cx="57" cy="166" r="8" />
      </svg>
    </div>
  );
}

export default function PatientOverviewPage() {
  const { user, recordState, historyState } = useOutletContext();
  const firstName = user.name.trim().split(/\s+/)[0];
  const record = recordCard(recordState);
  const history = historyCards(historyState);

  return (
    <>
      <section className="ws-hero" aria-labelledby="ws-page-title">
        <div className="ws-hero-copy">
          <p className="ws-eyebrow">YOUR CARE WORKSPACE</p>
          <h1 id="ws-page-title" tabIndex={-1}>Welcome, {firstName}.</h1>
          <p className="ws-hero-kicker">Healthcare Agent Safety Framework · Patient portal</p>
          <p>
            Review your medical record, ask your care team to correct anything
            that looks wrong, and follow each request through Physician review.
          </p>
          <Link className="ws-btn ws-btn-hero" to="/dashboard/patient/records">
            View my medical record <span aria-hidden="true">→</span>
          </Link>
        </div>
        <CareArt />
      </section>

      <div className="ws-stat-grid" aria-label="Your care summary">
        <StatCard icon="pulse" label="MEDICAL RECORD" note={record.note} textValue tone={1} value={record.value} />
        <StatCard icon="records" label="CORRECTION REQUESTS" note={history.total.note} tone={2} value={history.total.value} />
        <StatCard icon="pending" label="PENDING REVIEW" note={history.pending.note} tone={3} value={history.pending.value} />
        <StatCard icon="permissions" label="CURRENT CORRECTION STATUS" note={history.latest.note} textValue tone={4} value={history.latest.value} />
      </div>

      <h2 className="ws-section-title">Your care tools</h2>
      <div className="ws-module-grid">
        <ModuleLink
          description="See your record and request a treatment correction."
          icon="records"
          title="My Medical Records"
          to="/dashboard/patient/records"
          tone={1}
        />
        <ModuleLink
          description="Follow each correction through Physician review."
          icon="history"
          meta={historyState.status === "ready" ? `${historyState.items.length} request(s)` : null}
          title="AI Request History"
          to="/dashboard/patient/history"
          tone={2}
        />
        <ModuleLink
          description="Guarded help with the correction process — coming next phase."
          icon="assistant"
          title="Ask AI Assistant"
          to="/dashboard/patient/assistant"
          tone={3}
        />
        <ModuleLink
          description="Your verified account details and what you can do."
          icon="profile"
          title="My Profile"
          to="/dashboard/patient/profile"
          tone={4}
        />
      </div>
    </>
  );
}
