import { useEffect, useId, useRef, useState } from "react";
import { Link, useOutletContext } from "react-router-dom";

import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import {
  Badge,
  Chip,
  EmptyMessage,
  ErrorMessage,
  LoadingMessage,
  Panel,
} from "../../shared/workspaceUi.jsx";
import { submitTreatmentCorrection } from "./patientApi.js";
import {
  MAX_PROPOSED_TREATMENT_LENGTH,
  describeSubmissionError,
  findBlockingRequest,
  formatDateTime,
  parseSubmissionResponse,
  validateProposedTreatment,
} from "./patientCorrection.js";

const historyPath = "/dashboard/patient/history";

function SubmissionResult({ result }) {
  const resultRef = useRef(null);

  useEffect(() => {
    resultRef.current?.focus();
  }, [result]);

  if (result.type === "success") {
    return (
      <div className="ws-callout ws-callout-success" ref={resultRef} role="status" tabIndex={-1}>
        <strong>Correction request #{result.requestId} submitted</strong>
        <p>
          It is <Badge tone="pending">Pending review</Badge> by your assigned
          Physician. Your medical record has not been changed and will only
          change if the Physician approves the request.
        </p>
        <Link className="ws-link-button" to={historyPath}>Track it in Request History</Link>
      </div>
    );
  }

  const tone = result.outcome === "uncertain" || result.outcome === "conflict" ? "warning" : "error";
  return (
    <div className={`ws-callout ws-callout-${tone}`} ref={resultRef} role="alert" tabIndex={-1}>
      <strong>{result.title}</strong>
      <p>{result.message}</p>
      {(result.outcome === "uncertain" || result.outcome === "conflict") && (
        <Link className="ws-link-button" to={historyPath}>Open Request History</Link>
      )}
    </div>
  );
}

function CorrectionGate({ historyState, onRetryHistory }) {
  if (historyState.status === "loading") {
    return <LoadingMessage>Checking for requests already in review…</LoadingMessage>;
  }
  if (historyState.status === "error") {
    return (
      <ErrorMessage
        message="We couldn't check whether you already have a request in review, so new corrections are paused. Reload your requests to continue."
        onRetry={onRetryHistory}
        retryLabel="Reload requests"
      />
    );
  }
  const blocking = historyState.status === "ready" ? findBlockingRequest(historyState.items) : null;
  if (!blocking) return null;

  return (
    <div className="ws-callout ws-callout-info" role="status">
      <strong>Request #{blocking.request_id} is still in review</strong>
      <p>
        You can submit a new correction after your Physician has reviewed it.
        {blocking.created_at && <> It was submitted {formatDateTime(blocking.created_at)}.</>}
      </p>
      <Link className="ws-link-button" to={historyPath}>View request status</Link>
    </div>
  );
}

function CorrectionForm({ session, currentTreatment, historyState, onSubmitted, onRetryHistory }) {
  const [step, setStep] = useState("edit");
  const [proposal, setProposal] = useState("");
  const [validationError, setValidationError] = useState("");
  const [result, setResult] = useState(null);
  const submittingRef = useRef(false);
  const reviewHeadingRef = useRef(null);
  const textareaRef = useRef(null);
  const fieldId = useId();

  useEffect(() => {
    if (step === "review") reviewHeadingRef.current?.focus();
  }, [step]);

  const historyAllowsSubmission =
    historyState.status === "forbidden" ||
    (historyState.status === "ready" && !findBlockingRequest(historyState.items));

  function handleReview(event) {
    event.preventDefault();
    const error = validateProposedTreatment(proposal, currentTreatment);
    setValidationError(error);
    if (error) {
      textareaRef.current?.focus();
      return;
    }
    setResult(null);
    setStep("review");
  }

  async function handleConfirm() {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setStep("submitting");
    try {
      const response = parseSubmissionResponse(
        await submitTreatmentCorrection(session.access_token, proposal.trim()),
      );
      if (!response) {
        throw Object.assign(new Error("Unexpected confirmation."), { kind: "invalid" });
      }
      setResult({ type: "success", requestId: response.request_id });
      setProposal("");
      setValidationError("");
      onSubmitted();
    } catch (error) {
      const description = describeSubmissionError(error);
      setResult({ type: "error", ...description });
      // Refresh (never resubmit) so the Patient sees whether anything was recorded.
      if (description.outcome === "uncertain" || description.outcome === "conflict") {
        onSubmitted();
      }
    } finally {
      submittingRef.current = false;
      setStep("edit");
    }
  }

  return (
    <div className="ws-stack">
      {result && <SubmissionResult result={result} />}

      <CorrectionGate historyState={historyState} onRetryHistory={onRetryHistory} />

      {historyAllowsSubmission && step === "edit" && (
        <form className="ws-form" noValidate onSubmit={handleReview}>
          <div className="ws-field">
            <span id={`${fieldId}-current-label`}>Current treatment (read-only)</span>
            <p className="ws-readonly" aria-labelledby={`${fieldId}-current-label`}>
              {currentTreatment ?? "Not provided"}
            </p>
          </div>
          <div className="ws-field">
            <label htmlFor={`${fieldId}-proposal`}>Proposed treatment</label>
            <textarea
              aria-describedby={`${fieldId}-hint ${fieldId}-count${validationError ? ` ${fieldId}-error` : ""}`}
              aria-invalid={validationError ? "true" : "false"}
              id={`${fieldId}-proposal`}
              maxLength={MAX_PROPOSED_TREATMENT_LENGTH}
              onChange={(event) => {
                setProposal(event.target.value);
                if (validationError) setValidationError("");
              }}
              ref={textareaRef}
              rows={4}
              value={proposal}
            />
            <div className="ws-field-meta">
              <span id={`${fieldId}-hint`}>Describe only the treatment entry. Use fictional data in this demonstration.</span>
              <span id={`${fieldId}-count`}>{proposal.length} / {MAX_PROPOSED_TREATMENT_LENGTH}</span>
            </div>
            {validationError && (
              <p className="ws-field-error" id={`${fieldId}-error`} role="alert">{validationError}</p>
            )}
          </div>
          <div className="ws-form-actions">
            <button className="ws-btn ws-btn-primary" type="submit">Review request</button>
          </div>
        </form>
      )}

      {historyAllowsSubmission && step !== "edit" && (
        <div className="ws-form ws-review" aria-busy={step === "submitting"}>
          <h3 ref={reviewHeadingRef} tabIndex={-1}>Please confirm your correction request</h3>
          <dl className="ws-compare">
            <div>
              <dt>Current treatment</dt>
              <dd>{currentTreatment ?? "Not provided"}</dd>
            </div>
            <div>
              <dt>Proposed treatment</dt>
              <dd>{proposal.trim()}</dd>
            </div>
          </dl>
          <ul className="ws-points">
            <li>This sends a request to your assigned Physician. It does not change your record.</li>
            <li>You can't submit another treatment correction until this one is reviewed.</li>
          </ul>
          <div className="ws-form-actions">
            <button
              className="ws-btn ws-btn-secondary"
              disabled={step === "submitting"}
              onClick={() => setStep("edit")}
              type="button"
            >
              Go back and edit
            </button>
            <button
              className="ws-btn ws-btn-primary"
              disabled={step === "submitting"}
              onClick={handleConfirm}
              type="button"
            >
              {step === "submitting" ? "Submitting…" : "Confirm and submit"}
            </button>
          </div>
          {step === "submitting" && (
            <p className="ws-visually-hidden" role="status">Submitting your correction request…</p>
          )}
        </div>
      )}
    </div>
  );
}

export default function PatientRecordsPage() {
  const { user, session, recordState, historyState, retryRecord, refreshHistory } = useOutletContext();
  const canSubmit = user.permissions.includes("submit_record_correction");

  return (
    <>
      <PageHeader
        description="Your record exactly as stored by your care team. It can only be changed after Physician approval."
        eyebrow="YOUR HEALTH INFORMATION"
        icon="records"
        title="My Medical Records"
      />

      <div className="ws-split">
        <Panel
          aside={<Chip>Only visible to you</Chip>}
          eyebrow="MEDICAL RECORD"
          icon="pulse"
          title="Record details"
          titleId="record-details-title"
          tone={1}
        >
          {recordState.status === "loading" && <LoadingMessage>Loading your medical record securely…</LoadingMessage>}
          {recordState.status === "empty" && (
            <EmptyMessage>No medical record is linked to your account yet.</EmptyMessage>
          )}
          {recordState.status === "error" && (
            <ErrorMessage message={recordState.error} onRetry={retryRecord} />
          )}
          {recordState.status === "ready" && (
            <dl className="ws-details">
              <div>
                <dt>Age</dt>
                <dd>{recordState.record.age ?? "Not provided"}</dd>
              </div>
              <div>
                <dt>Medical condition</dt>
                <dd>{recordState.record.medical_condition ?? "Not provided"}</dd>
              </div>
              <div>
                <dt>Treatment</dt>
                <dd>{recordState.record.treatment ?? "Not provided"}</dd>
              </div>
            </dl>
          )}
        </Panel>

        <Panel
          eyebrow="HUMAN-IN-THE-LOOP"
          icon="permissions"
          subtitle="If your treatment is recorded incorrectly, describe the correct treatment. Your assigned Physician reviews every request — your record does not change unless they approve it."
          title="Request a treatment correction"
          titleId="correction-title"
          tone={2}
        >
          {recordState.status === "ready" && canSubmit && (
            <CorrectionForm
              currentTreatment={recordState.record.treatment}
              historyState={historyState}
              onRetryHistory={refreshHistory}
              onSubmitted={refreshHistory}
              session={session}
            />
          )}
          {recordState.status === "ready" && !canSubmit && (
            <EmptyMessage>
              Corrections are not available for your account. Contact your care team if something is incorrect.
            </EmptyMessage>
          )}
          {recordState.status !== "ready" && (
            <EmptyMessage>Corrections become available once your record has loaded.</EmptyMessage>
          )}
        </Panel>
      </div>
    </>
  );
}
