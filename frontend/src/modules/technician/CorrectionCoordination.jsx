import { useId, useRef, useState } from "react";

import { describeError } from "../../shared/taskApi.js";
import { Badge } from "../../shared/workspaceUi.jsx";
import { submitCorrectionAction } from "./technicianApi.js";
import {
  CORRECTION_ACTIONS,
  EMPTY_CORRECTION_FORM,
  availableCorrectionActions,
  buildCorrectionPayload,
  correctionWaitingNote,
  describeCorrectionResult,
  validateCorrectionForm,
} from "./correctionCoordination.js";
import { APPROVAL_META, MAX_INTERNAL_NOTE, MAX_PATIENT_MESSAGE, formatDateTime } from "./technician.js";

/**
 * Coordination for a medical-record correction. The Physician alone approves or rejects it: this panel shows
 * that decision (read-only) and offers only coordination actions. It never shows the correction text.
 */
export default function CorrectionCoordination({ task, token, onDone }) {
  const baseId = useId();
  const [form, setForm] = useState({ ...EMPTY_CORRECTION_FORM });
  const [touched, setTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [requestError, setRequestError] = useState("");
  const inFlight = useRef(false);

  const actions = availableCorrectionActions(task);
  const current = actions.some((a) => a.key === form.action) ? form : { ...form, action: "" };
  const action = CORRECTION_ACTIONS[current.action] ?? null;
  const declineReasons = task.correction.decline_reasons ?? [];
  const { errors, valid } = validateCorrectionForm(current, declineReasons);
  const approval = APPROVAL_META[task.correction.approval_status] ?? APPROVAL_META.none;
  const reviewedAt = formatDateTime(task.correction.reviewed_at);

  function choose(key) {
    setForm({ ...EMPTY_CORRECTION_FORM, action: key });
    setTouched(false);
    setRequestError("");
  }

  function update(patch) {
    setForm((f) => ({ ...f, ...patch }));
    setRequestError("");
  }

  async function handleSubmit(event) {
    event.preventDefault();
    if (inFlight.current) return;
    setTouched(true);
    if (!valid) return;
    inFlight.current = true;
    setSubmitting(true);
    setRequestError("");
    try {
      const response = await submitCorrectionAction(token, task.task_id, buildCorrectionPayload(current));
      setForm({ ...EMPTY_CORRECTION_FORM });
      setTouched(false);
      await onDone(describeCorrectionResult(response));
    } catch (error) {
      setRequestError(describeError(error));
      await onDone(null);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  const waiting = actions.length === 0 ? correctionWaitingNote(task) : "";

  return (
    <section className="ws-form tc-decision tc-corr" aria-label="Correction coordination">
      <h3 className="ws-section-title">Coordinate this correction</h3>

      <div className="ws-callout ws-callout-info tc-corr-status" role="status">
        <p>
          <strong>Physician review:</strong> <Badge tone={approval.tone}>{approval.label}</Badge>
          {reviewedAt ? <> · decided {reviewedAt}</> : null}
        </p>
        <p>
          Corrections reach you first. Put one on HOLD to ask the patient for more information or to request the
          assigned Physician's authorization, or decline it for an administrative reason. Only the Physician can
          approve or reject a correction, and you can close this task once they have decided. You never see or
          change the correction itself.
        </p>
      </div>

      {waiting && <p className="ws-form-note">{waiting}</p>}

      {actions.length > 0 && (
        <form noValidate onSubmit={handleSubmit} aria-busy={submitting} className="ws-form">
          <fieldset className="tc-choices tc-choices-3" disabled={submitting}>
            <legend className="ws-visually-hidden">Choose an action</legend>
            {actions.map((a) => (
              <label className={`tc-choice${current.action === a.key ? " is-selected" : ""}`} key={a.key}>
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

          {action && action.needsReason && (
            <>
              <p className="ws-callout ws-callout-warning" role="note">
                Administrative reasons only. Never decline a correction because of what it says medically: if it needs
                clinical judgement, request Physician authorization instead. Your reason and explanation are kept in the audit history.
              </p>
              <div className="ws-field">
                <label htmlFor={`${baseId}-reason`}>Reason (required)</label>
                <select
                  aria-describedby={touched && errors.reasonCode ? `${baseId}-reason-error` : undefined}
                  aria-invalid={touched && Boolean(errors.reasonCode)}
                  disabled={submitting}
                  id={`${baseId}-reason`}
                  onChange={(event) => update({ reasonCode: event.target.value })}
                  value={form.reasonCode}
                >
                  <option value="">Choose a reason…</option>
                  {declineReasons.map((reason) => (
                    <option key={reason.code} value={reason.code}>{reason.label}</option>
                  ))}
                </select>
                {touched && errors.reasonCode && (
                  <p className="ws-field-error" id={`${baseId}-reason-error`} role="alert">{errors.reasonCode}</p>
                )}
              </div>
            </>
          )}

          {action && (
            <>
              <fieldset className="tc-ap-section tc-ap-patient">
                <legend>
                  <span className="tc-ap-pill tc-ap-pill-patient">Visible to the patient</span>
                  Patient message
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
                    onChange={(event) => update({ patientMessage: event.target.value })}
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
                  Internal case note
                </legend>
                <div className="ws-field">
                  <label htmlFor={`${baseId}-note`}>{action.noteLabel}</label>
                  <p className="ws-form-note" id={`${baseId}-note-help`}>
                    Only you and the reviewing Physician can read this. Do not copy the correction text here.
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
            <button className={`ws-btn ${action?.kind === "DENY" ? "ws-btn-danger" : "ws-btn-primary"}`} disabled={submitting || !action} type="submit">
              {submitting ? "Saving…" : action ? action.submit : "Choose an action"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
