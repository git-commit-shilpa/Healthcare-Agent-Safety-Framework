import { useEffect, useId, useRef, useState } from "react";
import { Link, useOutletContext } from "react-router-dom";

import { dashboardPath } from "../../app/routeAccess.js";
import { describeError } from "../../shared/taskApi.js";
import { useRemote } from "../../shared/useRemote.js";
import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import {
  Badge,
  EmptyMessage,
  ErrorMessage,
  LoadingMessage,
  Panel,
  RefreshButton,
} from "../../shared/workspaceUi.jsx";
import {
  GROUPS,
  MAX_REASON,
  MAX_REPLY,
  appointmentStatus,
  bookingStep,
  buildBookingPayload,
  buildSummary,
  defaultGroup,
  describeBookingFailure,
  describeReplyFailure,
  earliestBookableDate,
  findSlot,
  formatCreated,
  groupAppointments,
  latestBookableDate,
  needsReplyCount,
  selectionStillAvailable,
  validateDate,
  validateReason,
  validateReply,
} from "./appointments.js";
import {
  cancelAppointment,
  getAppointment,
  getAvailability,
  listAppointments,
  listPhysicians,
  replyToAppointment,
  requestAppointment,
} from "./appointmentsApi.js";

const STEPS = [
  { key: "doctor", label: "Choose a doctor" },
  { key: "date", label: "Pick a date" },
  { key: "time", label: "Select a time" },
  { key: "details", label: "Review and send" },
];

function StepList({ current }) {
  const index = STEPS.findIndex((step) => step.key === current);
  return (
    <ol className="ap-steps" aria-label="Booking steps">
      {STEPS.map((step, i) => (
        <li
          aria-current={i === index ? "step" : undefined}
          className={`ap-step${i < index ? " is-done" : ""}${i === index ? " is-current" : ""}`}
          key={step.key}
        >
          <span className="ap-step-number" aria-hidden="true">{i < index ? "✓" : i + 1}</span>
          {step.label}
        </li>
      ))}
    </ol>
  );
}

function Notice({ notice }) {
  if (!notice) return null;
  return (
    <p
      className={`ws-callout ws-callout-${notice.tone}`}
      role={notice.urgent || notice.tone === "error" ? "alert" : "status"}
    >
      {notice.urgent && <strong>Urgent: </strong>}
      {notice.message}
    </p>
  );
}

function BookingFlow({ token, onSubmitted }) {
  const baseId = useId();
  const [physicianId, setPhysicianId] = useState("");
  const [date, setDate] = useState("");
  const [slotStart, setSlotStart] = useState("");
  const [reason, setReason] = useState("");
  const [phase, setPhase] = useState("edit"); // edit | review | done
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [result, setResult] = useState(null);
  const inFlight = useRef(false);

  const physicians = useRemote(() => listPhysicians(token), [token]);
  const dateError = date ? validateDate(date) : "";
  const canLoadSlots = Boolean(physicianId) && Boolean(date) && !dateError;
  const availability = useRemote(
    () => (canLoadSlots ? getAvailability(token, physicianId, date) : Promise.resolve(null)),
    [token, physicianId, date, canLoadSlots],
  );
  const slots = availability.status === "ready" && availability.data ? availability.data.slots : [];
  const physician = (physicians.data ?? []).find((p) => String(p.physician_id) === String(physicianId));
  const slot = findSlot(slots, slotStart);
  const step = bookingStep({ physicianId, date, slotStart });
  const reasonError = validateReason(reason);

  // Never keep a selection the server no longer offers (stale availability).
  useEffect(() => {
    if (slotStart && availability.status === "ready" && !selectionStillAvailable(slots, slotStart)) {
      setSlotStart("");
      setPhase("edit");
      setConfirmed(false);
      setNotice({ tone: "warning", message: "The time you chose is no longer available. Please choose another." });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availability.status, availability.data]);

  function change(setter) {
    return (value) => {
      setter(value);
      setNotice(null);
      setPhase("edit");
      setConfirmed(false);
    };
  }

  function chooseDoctor(value) {
    setPhysicianId(value);
    setSlotStart("");
    setNotice(null);
    setPhase("edit");
    setConfirmed(false);
  }

  function chooseDate(value) {
    setDate(value);
    setSlotStart("");
    setNotice(null);
    setPhase("edit");
    setConfirmed(false);
  }

  function goToReview(event) {
    event.preventDefault();
    if (!slot || reasonError) return;
    setNotice(null);
    setPhase("review");
  }

  async function send() {
    if (inFlight.current || !confirmed || !slot) return;
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const response = await requestAppointment(token, buildBookingPayload({ physicianId, slotStart, reason }));
      setResult(response);
      setPhase("done");
      await onSubmitted();
    } catch (error) {
      const failure = describeBookingFailure(error);
      setNotice({ tone: failure.tone, urgent: failure.urgent, message: failure.message });
      setPhase("edit");
      setConfirmed(false);
      if (failure.refreshSlots) {
        setSlotStart("");
        availability.reload();
      }
      if (failure.refreshList) await onSubmitted();
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function startOver() {
    setPhysicianId("");
    setDate("");
    setSlotStart("");
    setReason("");
    setConfirmed(false);
    setResult(null);
    setNotice(null);
    setPhase("edit");
  }

  if (phase === "done" && result) {
    const status = appointmentStatus(result.status);
    return (
      <div className="ap-done" role="status">
        <p className="ws-eyebrow">REQUEST SENT</p>
        <h3>Request ID #{result.request_id}</h3>
        <p><Badge tone={status.tone}>{status.label}</Badge></p>
        <p>{result.message}</p>
        <dl className="ap-summary">
          {buildSummary({ physician, slot, reason }).map((row) => (
            <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>
          ))}
        </dl>
        <div className="ws-form-actions">
          <button className="ws-btn ws-btn-secondary" onClick={startOver} type="button">Request another appointment</button>
        </div>
      </div>
    );
  }

  if (physicians.status === "loading" && !physicians.data) return <LoadingMessage />;
  if (physicians.status === "error") return <ErrorMessage message={physicians.error} onRetry={physicians.reload} />;
  if (physicians.status === "ready" && physicians.data.length === 0) {
    return (
      <EmptyMessage>
        No doctors are accepting appointment requests online right now. Please contact your care team.
      </EmptyMessage>
    );
  }

  if (phase === "review" && slot) {
    return (
      <div className="ap-review">
        <StepList current="details" />
        <h3>Please check your request</h3>
        <dl className="ap-summary">
          {buildSummary({ physician, slot, reason }).map((row) => (
            <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>
          ))}
        </dl>
        <p className="ws-callout ws-callout-info" role="note">
          Sending this creates a <strong>request</strong>. Your appointment is not booked until a Technician confirms it.
        </p>
        <Notice notice={notice} />
        <label className="ap-confirm" htmlFor={`${baseId}-confirm`}>
          <input
            checked={confirmed}
            disabled={busy}
            id={`${baseId}-confirm`}
            onChange={(event) => setConfirmed(event.target.checked)}
            type="checkbox"
          />
          I have checked these details and want to send this request.
        </label>
        <div className="ws-form-actions">
          <button className="ws-btn ws-btn-primary" disabled={!confirmed || busy} onClick={send} type="button">
            {busy ? "Sending…" : "Send request"}
          </button>
          <button className="ws-btn ws-btn-secondary" disabled={busy} onClick={() => setPhase("edit")} type="button">
            Back
          </button>
        </div>
      </div>
    );
  }

  return (
    <form className="ws-form" noValidate onSubmit={goToReview}>
      <StepList current={step} />

      <div className="ws-field">
        <label htmlFor={`${baseId}-doctor`}>Doctor</label>
        <select
          disabled={physicians.status !== "ready"}
          id={`${baseId}-doctor`}
          onChange={(event) => chooseDoctor(event.target.value)}
          value={physicianId}
        >
          <option value="">Choose a doctor…</option>
          {(physicians.data ?? []).map((p) => (
            <option key={p.physician_id} value={p.physician_id}>{p.name}</option>
          ))}
        </select>
      </div>

      <div className="ws-field">
        <label htmlFor={`${baseId}-date`}>Date</label>
        <input
          aria-describedby={dateError ? `${baseId}-date-error` : undefined}
          aria-invalid={Boolean(dateError)}
          disabled={!physicianId}
          id={`${baseId}-date`}
          max={latestBookableDate()}
          min={earliestBookableDate()}
          onChange={(event) => chooseDate(event.target.value)}
          type="date"
          value={date}
        />
        {dateError && <p className="ws-field-error" id={`${baseId}-date-error`} role="alert">{dateError}</p>}
      </div>

      <fieldset className="ap-times" disabled={!canLoadSlots}>
        <legend>Available times{physician?.timezone ? ` (${physician.timezone})` : ""}</legend>
        {!canLoadSlots && <p className="ws-form-note">Choose a doctor and a date to see available times.</p>}
        {canLoadSlots && availability.status === "loading" && <LoadingMessage />}
        {canLoadSlots && availability.status === "error" && (
          <ErrorMessage message={availability.error} onRetry={availability.reload} />
        )}
        {canLoadSlots && availability.status === "ready" && slots.length === 0 && (
          <EmptyMessage>No times are available on this date. Please try another day.</EmptyMessage>
        )}
        {slots.length > 0 && (
          <div className="ap-slots" role="radiogroup" aria-label="Available times">
            {slots.map((s) => (
              <label className={`ap-slot${slotStart === s.start ? " is-selected" : ""}`} key={s.start}>
                <input
                  checked={slotStart === s.start}
                  name={`${baseId}-slot`}
                  onChange={() => change(setSlotStart)(s.start)}
                  type="radio"
                  value={s.start}
                />
                <span>{s.local_label.split(", ").slice(-1)[0]}</span>
              </label>
            ))}
          </div>
        )}
        <p className="ws-form-note">Showing a time does not book it. Times can be taken by someone else before you send your request.</p>
      </fieldset>

      <div className="ws-field">
        <label htmlFor={`${baseId}-reason`}>Reason for the visit (optional)</label>
        <p className="ws-form-note" id={`${baseId}-reason-help`}>
          Keep it short, for example &quot;annual check-up&quot;. This is not for emergencies. If you may be in danger, call your local emergency number now.
        </p>
        <textarea
          aria-describedby={`${baseId}-reason-help`}
          id={`${baseId}-reason`}
          maxLength={MAX_REASON}
          onChange={(event) => change(setReason)(event.target.value)}
          rows={3}
          value={reason}
        />
        <div className="ws-field-meta"><span>{reason.length}/{MAX_REASON}</span></div>
        {reasonError && <p className="ws-field-error" role="alert">{reasonError}</p>}
      </div>

      <Notice notice={notice} />

      <div className="ws-form-actions">
        <button className="ws-btn ws-btn-primary" disabled={!slot || Boolean(reasonError)} type="submit">
          Review request
        </button>
      </div>
    </form>
  );
}

function Timeline({ entries }) {
  if (!entries?.length) return <p className="ws-form-note">No updates yet.</p>;
  return (
    <ol className="ap-timeline" aria-label="Updates">
      {entries.map((entry, i) => (
        <li key={`${entry.kind}-${i}`}>
          <strong>{entry.title}</strong>
          {entry.at && <span className="ap-time"> · {formatCreated(entry.at)}</span>}
          {entry.text && <p>{entry.text}</p>}
        </li>
      ))}
    </ol>
  );
}

function AppointmentCard({ item, token, onChanged }) {
  const status = appointmentStatus(item.status);
  const [mode, setMode] = useState(null); // null | "cancel" | "reply"
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [showUpdates, setShowUpdates] = useState(false);
  const [timeline, setTimeline] = useState({ status: "idle", entries: [] });
  const inFlight = useRef(false);
  const replyId = useId();

  async function toggleUpdates() {
    const next = !showUpdates;
    setShowUpdates(next);
    if (next) {
      setTimeline((t) => ({ ...t, status: "loading" }));
      try {
        const detail = await getAppointment(token, item.request_id);
        setTimeline({ status: "ready", entries: detail.timeline });
      } catch (loadError) {
        setTimeline({ status: "error", entries: [], message: describeError(loadError) });
      }
    }
  }

  async function run(action) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await action();
      setMode(null);
      setReply("");
    } catch (requestError) {
      setError(describeReplyFailure(requestError));
    } finally {
      await onChanged();
      inFlight.current = false;
      setBusy(false);
    }
  }

  function handleReply() {
    const problem = validateReply(reply);
    if (problem) {
      setError({ urgent: false, message: problem });
      return;
    }
    run(() => replyToAppointment(token, item.request_id, reply));
  }

  return (
    <li className="ap-card">
      <div className="ap-card-top">
        <div>
          <p className="ws-eyebrow">REQUEST #{item.request_id}</p>
          <h3>{item.physician_name ?? "Doctor"}</h3>
          <p className="ap-when">{item.local_label}</p>
        </div>
        <Badge tone={status.tone}>{status.label}</Badge>
      </div>
      <p className="ap-meta">{status.help}</p>
      {item.reason && <p className="ap-meta">You wrote: {item.reason}</p>}
      {item.patient_message && (
        <p className="ap-message"><strong>Message from your care team:</strong> {item.patient_message}</p>
      )}

      {error && (
        <p className="ws-callout ws-callout-error" role="alert">
          {error.urgent && <strong>Urgent: </strong>}{error.message}
        </p>
      )}

      {mode === "reply" && (
        <div className="ws-form">
          <div className="ws-field">
            <label htmlFor={replyId}>Your reply</label>
            <textarea
              disabled={busy}
              id={replyId}
              maxLength={MAX_REPLY}
              onChange={(event) => setReply(event.target.value)}
              rows={3}
              value={reply}
            />
          </div>
          <div className="ws-form-actions">
            <button className="ws-btn ws-btn-primary" disabled={busy} onClick={handleReply} type="button">
              {busy ? "Sending…" : "Send reply"}
            </button>
            <button className="ws-btn ws-btn-secondary" disabled={busy} onClick={() => setMode(null)} type="button">Not now</button>
          </div>
        </div>
      )}

      {mode === "cancel" && (
        <div className="ws-form-actions" role="group" aria-label="Confirm cancellation">
          <span>Cancel this request?</span>
          <button className="ws-btn ws-btn-danger" disabled={busy} onClick={() => run(() => cancelAppointment(token, item.request_id))} type="button">
            {busy ? "Cancelling…" : "Yes, cancel it"}
          </button>
          <button className="ws-btn ws-btn-secondary" disabled={busy} onClick={() => setMode(null)} type="button">Keep it</button>
        </div>
      )}

      <div className="ap-actions">
        {mode === null && item.can_add_info && (
          <button className="ws-btn ws-btn-primary" onClick={() => setMode("reply")} type="button">Reply to your care team</button>
        )}
        {mode === null && item.can_cancel && (
          <button className="ws-btn ws-btn-secondary" onClick={() => setMode("cancel")} type="button">Cancel request</button>
        )}
        <button aria-expanded={showUpdates} className="ws-btn ws-btn-secondary" onClick={toggleUpdates} type="button">
          {showUpdates ? "Hide updates" : "View updates"}
        </button>
      </div>

      {showUpdates && (
        <div className="ap-updates">
          {timeline.status === "loading" && <LoadingMessage />}
          {timeline.status === "error" && <p className="ws-callout ws-callout-error" role="alert">{timeline.message}</p>}
          {timeline.status === "ready" && <Timeline entries={timeline.entries} />}
        </div>
      )}
    </li>
  );
}

function AppointmentList({ items, token, onChanged }) {
  const grouped = groupAppointments(items);
  const [tab, setTab] = useState(null);
  const active = tab ?? defaultGroup(grouped);
  const group = GROUPS.find((g) => g.key === active) ?? GROUPS[0];
  const shown = grouped[group.key];

  return (
    <>
      <div className="ap-tabs" role="tablist" aria-label="Appointment groups">
        {GROUPS.map((g) => (
          <button
            aria-selected={g.key === group.key}
            className={`ap-tab${g.key === group.key ? " is-active" : ""}`}
            id={`ap-tab-${g.key}`}
            key={g.key}
            onClick={() => setTab(g.key)}
            role="tab"
            type="button"
          >
            {g.label} <span className="ap-count">{grouped[g.key].length}</span>
          </button>
        ))}
      </div>
      <div role="tabpanel" aria-labelledby={`ap-tab-${group.key}`}>
        {shown.length === 0 ? (
          <EmptyMessage>{group.empty}</EmptyMessage>
        ) : (
          <ul className="ap-list" aria-label={`${group.label} appointments`}>
            {shown.map((item) => (
              <AppointmentCard item={item} key={item.request_id} onChanged={onChanged} token={token} />
            ))}
          </ul>
        )}
      </div>
    </>
  );
}

export default function PatientAppointmentsPage() {
  const { session } = useOutletContext();
  const token = session.access_token;
  const appointments = useRemote(() => listAppointments(token), [token]);
  const items = appointments.status === "ready" ? appointments.data : [];
  const replies = needsReplyCount(items);

  return (
    <>
      <PageHeader
        actions={
          <>
            <RefreshButton busy={appointments.status === "loading"} label="Refresh" onClick={appointments.reload} />
            <Link className="ws-btn ws-btn-secondary" to={dashboardPath("patient", "requests")}>
              Other requests
            </Link>
          </>
        }
        description="Request a visit with one of our doctors, then track whether it is confirmed. A request is not a booking until a Technician confirms it."
        eyebrow="YOUR CARE"
        icon="calendar"
        title="Appointments"
      />

      {appointments.status === "unavailable" && (
        <p className="ws-callout ws-callout-info" role="status">
          Appointment requests are not available yet. Please check back soon. For anything urgent, contact your care team directly.
        </p>
      )}

      {appointments.status !== "unavailable" && replies > 0 && (
        <p className="ws-callout ws-callout-warning" role="status">
          {replies === 1 ? "One request needs" : `${replies} requests need`} a reply from you.
        </p>
      )}

      {appointments.status !== "unavailable" && (
        <div className="ap-grid">
          <Panel eyebrow="NEW REQUEST" icon="calendar" title="Request an appointment" titleId="ap-new-title" tone={1}>
            <BookingFlow onSubmitted={appointments.reload} token={token} />
          </Panel>

          <Panel
            eyebrow="YOUR APPOINTMENTS"
            icon="history"
            subtitle="Messages from your care team appear here."
            title="Your appointments"
            titleId="ap-list-title"
            tone={2}
          >
            {appointments.status === "loading" && !appointments.data && <LoadingMessage />}
            {appointments.status === "error" && (
              <ErrorMessage message={appointments.error} onRetry={appointments.reload} />
            )}
            {appointments.status === "ready" && (
              <AppointmentList items={items} onChanged={appointments.reload} token={token} />
            )}
          </Panel>
        </div>
      )}
    </>
  );
}
