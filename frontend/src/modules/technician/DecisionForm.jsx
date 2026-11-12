import { useId, useRef, useState } from "react";

import { describeError, isUnconfirmed } from "../../shared/taskApi.js";
import { submitDecision } from "./technicianApi.js";
import {
  EMPTY_DECISION_FORM,
  MAX_INTERNAL_NOTE,
  MAX_PATIENT_MESSAGE,
  allowWarning,
  availableDecisions,
  buildDecisionPayload,
  describeDecisionResult,
  validateDecisionForm,
} from "./technician.js";

const DECISION_COPY = {
  ALLOW: { title: "ALLOW", hint: "Proceed with this request." },
  BLOCK: { title: "BLOCK", hint: "Decline it. It cannot be processed." },
  HOLD: { title: "HOLD", hint: "Pause to ask the patient, or request physician approval." },
};

/** What the patient-facing message means for the chosen decision (wording only; the rules live in technician.js). */
function messageCopy(decision, holdReason) {
  if (decision === "BLOCK") {
    return { label: "Reason shown to the patient", help: "Explain clearly why this request cannot be processed." };
  }
  if (decision === "HOLD" && holdReason === "needs_information") {
    return { label: "Question for the patient", help: "Say exactly what information you need. The patient can reply." };
  }
  if (decision === "HOLD") {
    return { label: "Message to the patient", help: "Tell the patient a physician will review this request." };
  }
  return { label: "Message to the patient", help: "Optional. Confirm the outcome in plain language." };
}

/**
 * The Technician only chooses; the backend guardrails make the final decision and the page
 * shows exactly what was applied. BLOCK and HOLD need an internal reason and a patient message.
 */
export default function DecisionForm({ task, token, onDone }) {
  const baseId = useId();
  const [form, setForm] = useState({ ...EMPTY_DECISION_FORM });
  const [touched, setTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [requestError, setRequestError] = useState("");
  const inFlight = useRef(false); // blocks double submits even before React re-renders

  const decisions = availableDecisions(task);
  // A selection that is no longer allowed (the task changed under us) counts as nothing selected.
  const current = decisions.includes(form.decision) ? form : { ...form, decision: "", holdReason: "" };
  const { errors, valid } = validateDecisionForm(current);
  const showErrors = touched;
  const warning = current.decision === "ALLOW" ? allowWarning(task) : null;
  const needsReasons = current.decision === "BLOCK" || current.decision === "HOLD";
  const copy = messageCopy(current.decision, current.holdReason);

  function update(patch) {
    setForm((current) => ({ ...current, ...patch }));
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
      const response = await submitDecision(token, task.task_id, buildDecisionPayload(current));
      setForm({ ...EMPTY_DECISION_FORM });
      setTouched(false);
      await onDone(describeDecisionResult(response));
    } catch (error) {
      setRequestError(
        isUnconfirmed(error)
          ? `${describeError(error)} The task has been reloaded.`
          : describeError(error),
      );
      await onDone(null);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }

  if (decisions.length === 0) return null;

  return (
    <form className="ws-form tc-decision" noValidate onSubmit={handleSubmit} aria-busy={submitting}>
      <h3 className="ws-section-title">Your decision</h3>

      <fieldset className="tc-choices tc-choices-3" disabled={submitting}>
        <legend className="ws-visually-hidden">Choose a decision</legend>
        {decisions.map((decision) => (
          <label className={`tc-choice${current.decision === decision ? " is-selected" : ""}`} key={decision}>
            <input
              checked={current.decision === decision}
              name={`${baseId}-decision`}
              onChange={() => update({ decision, holdReason: decision === "HOLD" ? current.holdReason : "" })}
              type="radio"
              value={decision}
            />
            <span>
              <strong>{DECISION_COPY[decision].title}</strong>
              <span>{DECISION_COPY[decision].hint}</span>
            </span>
          </label>
        ))}
      </fieldset>
      {showErrors && errors.decision && <p className="ws-field-error" role="alert">{errors.decision}</p>}

      {warning && <p className="ws-callout ws-callout-warning" role="status">{warning}</p>}

      {current.decision === "HOLD" && (
        <div className="ws-field">
          <label htmlFor={`${baseId}-hold`}>Why is it on hold?</label>
          <select
            aria-describedby={showErrors && errors.holdReason ? `${baseId}-hold-error` : undefined}
            aria-invalid={showErrors && Boolean(errors.holdReason)}
            disabled={submitting}
            id={`${baseId}-hold`}
            onChange={(event) => update({ holdReason: event.target.value })}
            value={current.holdReason}
          >
            <option value="">Choose a reason…</option>
            <option value="needs_information">Waiting for more information from the patient</option>
            <option value="physician_approval">Needs physician approval</option>
          </select>
          {showErrors && errors.holdReason && (
            <p className="ws-field-error" id={`${baseId}-hold-error`} role="alert">{errors.holdReason}</p>
          )}
          {current.holdReason === "physician_approval" && (
            <p className="ws-form-note">
              This sends an approval request to the assigned physician. You cannot approve it yourself.
            </p>
          )}
        </div>
      )}

      <fieldset className="tc-ap-section tc-ap-patient">
        <legend>
          <span className="tc-ap-pill tc-ap-pill-patient">Visible to the patient</span>
          Patient message
        </legend>
      <div className="ws-field">
        <label htmlFor={`${baseId}-message`}>
          {copy.label}{needsReasons ? " (required)" : " (optional)"}
        </label>
        <p className="ws-form-note" id={`${baseId}-message-help`}>
          {copy.help} Keep internal reasoning and other people&apos;s details out of it.
        </p>
        <textarea
          aria-describedby={`${baseId}-message-help${showErrors && errors.patientMessage ? ` ${baseId}-message-error` : ""}`}
          aria-invalid={showErrors && Boolean(errors.patientMessage)}
          disabled={submitting}
          id={`${baseId}-message`}
          maxLength={MAX_PATIENT_MESSAGE}
          onChange={(event) => update({ patientMessage: event.target.value })}
          rows={3}
          value={form.patientMessage}
        />
        <div className="ws-field-meta"><span>{form.patientMessage.length}/{MAX_PATIENT_MESSAGE}</span></div>
        {showErrors && errors.patientMessage && (
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
        <label htmlFor={`${baseId}-note`}>
          Internal case note{needsReasons ? " (required)" : " (optional)"}
        </label>
        <p className="ws-form-note" id={`${baseId}-note-help`}>
          Visible only to you and the reviewing physician. Patients never see this.
        </p>
        <textarea
          aria-describedby={`${baseId}-note-help${showErrors && errors.internalNote ? ` ${baseId}-note-error` : ""}`}
          aria-invalid={showErrors && Boolean(errors.internalNote)}
          disabled={submitting}
          id={`${baseId}-note`}
          maxLength={MAX_INTERNAL_NOTE}
          onChange={(event) => update({ internalNote: event.target.value })}
          rows={4}
          value={form.internalNote}
        />
        <div className="ws-field-meta"><span>{form.internalNote.length}/{MAX_INTERNAL_NOTE}</span></div>
        {showErrors && errors.internalNote && (
          <p className="ws-field-error" id={`${baseId}-note-error`} role="alert">{errors.internalNote}</p>
        )}
      </div>

      </fieldset>

      {requestError && <p className="ws-callout ws-callout-error" role="alert">{requestError}</p>}
      <div className="ws-form-actions">
        <button className="ws-btn ws-btn-primary" disabled={submitting || !current.decision} type="submit">
          {submitting ? "Recording…" : "Record decision"}
        </button>
      </div>
      <p className="ws-form-note">
        The safety framework checks every decision independently. If it cannot be applied, you will see why and nothing changes.
      </p>
    </form>
  );
}
