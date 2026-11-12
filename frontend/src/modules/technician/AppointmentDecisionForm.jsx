import { useEffect, useId, useRef, useState } from "react";

import { describeError } from "../../shared/taskApi.js";
import { useRemote } from "../../shared/useRemote.js";
import { LoadingMessage } from "../../shared/workspaceUi.jsx";
import {
  EMPTY_APPOINTMENT_FORM,
  availableAppointmentActions,
  buildAppointmentPayload,
  defaultConfirmMessage,
  describeAppointmentFailure,
  describeAppointmentResult,
  internalDecisionLabel,
  shouldCheckSlot,
  slotCheckView,
  validateAppointmentForm,
  APPOINTMENT_ACTIONS,
} from "./appointmentDecision.js";
import { checkAppointmentSlot, submitDecision } from "./technicianApi.js";
import { MAX_INTERNAL_NOTE, MAX_PATIENT_MESSAGE } from "./technician.js";

/**
 * Context-specific decision form for an appointment request. The Technician picks an action;
 * the backend guardrail still evaluates the matching ALLOW / BLOCK / HOLD and the database
 * re-checks availability and reserves the slot atomically on confirmation.
 */
export default function AppointmentDecisionForm({ task, token, onDone }) {
  const baseId = useId();
  const [form, setForm] = useState({ ...EMPTY_APPOINTMENT_FORM });
  const [messageEdited, setMessageEdited] = useState(false);
  const [touched, setTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [requestError, setRequestError] = useState("");
  const inFlight = useRef(false);

  const actions = availableAppointmentActions(task);
  const current = actions.some((a) => a.key === form.action) ? form : { ...form, action: "" };
  const action = APPOINTMENT_ACTIONS[current.action] ?? null;
  const { errors, valid } = validateAppointmentForm(current);

  const check = useRemote(
    () => (shouldCheckSlot(task) ? checkAppointmentSlot(token, task.task_id) : Promise.resolve(null)),
    [token, task.task_id, task.status],
  );
  const slot = slotCheckView(check.status === "ready" ? check.data : null);
  const confirmBlocked = current.action === "confirm" && slot.blocksConfirm;

  // Offer the standard confirmation wording until the Technician edits it.
  useEffect(() => {
    if (current.action === "confirm" && !messageEdited && !form.patientMessage) {
      setForm((f) => ({ ...f, patientMessage: defaultConfirmMessage(task.appointment) }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current.action]);

  function choose(key) {
    setForm((f) => ({
      ...f,
      action: key,
      // Switching away from the pre-filled confirmation text must not leave it behind.
      patientMessage: !messageEdited ? "" : f.patientMessage,
    }));
    setRequestError("");
    setTouched(false);
  }

  function update(patch) {
    setForm((f) => ({ ...f, ...patch }));
    setRequestError("");
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current) return;
    setTouched(true);
    if (!valid || confirmBlocked) return;
    inFlight.current = true;
    setSubmitting(true);
    setRequestError("");
    try {
      const response = await submitDecision(token, task.task_id, buildAppointmentPayload(current));
      const result = describeAppointmentResult(response, current.action);
      setForm({ ...EMPTY_APPOINTMENT_FORM });
      setMessageEdited(false);
      setTouched(false);
      await onDone(result);
    } catch (error) {
      const failure = describeAppointmentFailure(error);
      setRequestError(failure.message || describeError(error));
      if (failure.recheck) check.reload();
      await onDone(null);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  if (actions.length === 0) return null;

  return (
    <form className="ws-form tc-decision tc-ap-form" noValidate onSubmit={handleSubmit} aria-busy={submitting}>
      <h3 className="ws-section-title">Decide on this request</h3>

      {shouldCheckSlot(task) && (
        <div className="tc-ap-check">
          {check.status === "loading" && !check.data && <LoadingMessage>Checking the doctor&apos;s availability…</LoadingMessage>}
          {check.status === "error" && (
            <p className="ws-callout ws-callout-warning" role="status">
              Could not check availability right now. It is checked again when you confirm.
            </p>
          )}
          {check.status === "ready" && slot.message && (
            <p className={`ws-callout ws-callout-${slot.tone}`} role="status">{slot.message}</p>
          )}
          <button className="ws-btn ws-btn-secondary" disabled={check.status === "loading" || submitting} onClick={check.reload} type="button">
            Re-check availability
          </button>
        </div>
      )}

      <fieldset className="tc-choices" disabled={submitting}>
        <legend className="ws-visually-hidden">Choose an action</legend>
        {actions.map((a) => (
          <label className={`tc-choice tc-ap-choice tc-ap-${a.key}${current.action === a.key ? " is-selected" : ""}`} key={a.key}>
            <input
              checked={current.action === a.key}
              name={`${baseId}-action`}
              onChange={() => choose(a.key)}
              type="radio"
              value={a.key}
            />
            <span>
              <strong>{a.title}</strong>
              <span>{a.description}</span>
            </span>
          </label>
        ))}
      </fieldset>
      {touched && errors.action && <p className="ws-field-error" role="alert">{errors.action}</p>}

      {confirmBlocked && (
        <p className="ws-callout ws-callout-warning" role="alert">
          This time cannot be confirmed. Choose Reject Request or Request More Information instead.
        </p>
      )}

      {action && (
        <>
          <fieldset className="tc-ap-section tc-ap-patient">
            <legend>
              <span className="tc-ap-pill tc-ap-pill-patient">Visible to the patient</span>
              Message to the patient
            </legend>
            <div className="ws-field">
              <label htmlFor={`${baseId}-message`}>{action.messageLabel}</label>
              <p className="ws-form-note" id={`${baseId}-message-help`}>{action.messageHelp}</p>
              <textarea
                aria-describedby={`${baseId}-message-help${touched && errors.patientMessage ? ` ${baseId}-message-error` : ""}`}
                aria-invalid={touched && Boolean(errors.patientMessage)}
                disabled={submitting}
                id={`${baseId}-message`}
                maxLength={MAX_PATIENT_MESSAGE}
                onChange={(event) => {
                  setMessageEdited(true);
                  update({ patientMessage: event.target.value });
                }}
                rows={3}
                value={form.patientMessage}
              />
              <div className="ws-field-meta"><span>{form.patientMessage.length}/{MAX_PATIENT_MESSAGE}</span></div>
              {touched && errors.patientMessage && (
                <p className="ws-field-error" id={`${baseId}-message-error`} role="alert">{errors.patientMessage}</p>
              )}
            </div>
          </fieldset>

          <fieldset className="tc-ap-section tc-ap-internal">
            <legend>
              <span className="tc-ap-pill tc-ap-pill-internal">Internal · patients never see this</span>
              Internal case notes
            </legend>
            <div className="ws-field">
              <label htmlFor={`${baseId}-note`}>{action.noteLabel}</label>
              <p className="ws-form-note" id={`${baseId}-note-help`}>
                Only authorized Technicians and Physicians can read this. Record your reasoning here, not in the patient message.
              </p>
              <textarea
                aria-describedby={`${baseId}-note-help${touched && errors.internalNote ? ` ${baseId}-note-error` : ""}`}
                aria-invalid={touched && Boolean(errors.internalNote)}
                disabled={submitting}
                id={`${baseId}-note`}
                maxLength={MAX_INTERNAL_NOTE}
                onChange={(event) => update({ internalNote: event.target.value })}
                rows={3}
                value={form.internalNote}
              />
              <div className="ws-field-meta"><span>{form.internalNote.length}/{MAX_INTERNAL_NOTE}</span></div>
              {touched && errors.internalNote && (
                <p className="ws-field-error" id={`${baseId}-note-error`} role="alert">{errors.internalNote}</p>
              )}
            </div>
          </fieldset>
        </>
      )}

      {requestError && <p className="ws-callout ws-callout-error" role="alert">{requestError}</p>}

      <div className="ws-form-actions">
        <button
          className={`ws-btn ${current.action === "reject" ? "ws-btn-danger" : "ws-btn-primary"}`}
          disabled={submitting || !action || confirmBlocked}
          type="submit"
        >
          {submitting ? "Saving…" : action ? action.submit : "Choose an action"}
        </button>
      </div>
      {action && (
        <p className="ws-form-note">
          Recorded internally as <strong>{internalDecisionLabel(current.action)}</strong>. The safety framework checks every
          decision independently; if it cannot be applied you will see why and nothing changes.
        </p>
      )}
    </form>
  );
}
