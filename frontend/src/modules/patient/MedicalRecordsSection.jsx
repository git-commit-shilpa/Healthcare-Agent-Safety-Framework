import { useEffect, useId, useRef, useState } from "react";
import { Link, useLocation, useOutletContext } from "react-router-dom";

import { PageHeader } from "../../shared/WorkspaceShell.jsx";
import {
  Badge,
  Chip,
  EmptyMessage,
  ErrorMessage,
  LoadingMessage,
  Panel,
  RefreshButton,
} from "../../shared/workspaceUi.jsx";
import { submitTreatmentCorrection } from "./patientApi.js";
import {
  MAX_PROPOSED_TREATMENT_LENGTH,
  describeApprovalStatus,
  describeDecision,
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
  const location = useLocation();
  const canSubmit = user.permissions.includes("submit_record_correction");
  const canUseAssistant = user.permissions.includes("request_ai_assistance");
  const useClassicFallback =
    canSubmit &&
    (!canUseAssistant ||
      new URLSearchParams(location.search).get("correctionFallback") === "1");

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
            <div className="ws-stack">
              {canUseAssistant && !useClassicFallback && (
                <>
                  <p>
                    <Link className="ws-btn ws-btn-primary" to="/dashboard/patient/assistant">
                      Request a Correction
                    </Link>
                  </p>
                  <p className="ws-form-note">
                    Tell the AI Assistant what needs correcting. It guides you through a secure review and
                    confirmation step.
                  </p>
                </>
              )}
              {useClassicFallback && (
                <>
                  <p className="ws-form-note">
                    {canUseAssistant
                      ? "The AI-assisted correction process is temporarily unavailable. You can use this secure form instead."
                      : "The AI-assisted correction process is unavailable for your account. You can use this secure form instead."}
                    {" "}Your assigned Physician will still review the request before any record changes.
                  </p>
                  {canUseAssistant && (
                    <p>
                      <Link className="ws-link-button" to="/dashboard/patient/assistant">
                        Return to the AI Assistant
                      </Link>
                    </p>
                  )}
                </>
              )}
              {useClassicFallback && (
                <CorrectionForm
                  currentTreatment={recordState.record.treatment}
                  historyState={historyState}
                  onRetryHistory={refreshHistory}
                  onSubmitted={refreshHistory}
                  session={session}
                />
              )}
            </div>
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

      <Panel
        aside={
          historyState.status !== "forbidden" && (
            <RefreshButton
              busy={historyState.status === "loading"}
              label="Refresh requests"
              onClick={refreshHistory}
            />
          )
        }
        eyebrow="YOUR ACTIVITY"
        icon="history"
        subtitle="Track each treatment correction request and its Physician review."
        title="My Correction Requests"
        titleId="correction-requests-title"
        tone={3}
      >
        {historyState.status === "forbidden" && (
          <EmptyMessage>Correction request history is not available for your account.</EmptyMessage>
        )}
        {historyState.status === "loading" && (
          <LoadingMessage>Loading your correction requests securely…</LoadingMessage>
        )}
        {historyState.status === "error" && (
          <ErrorMessage
            message={historyState.error || "Could not load your correction requests."}
            onRetry={refreshHistory}
            retryLabel="Reload requests"
          />
        )}
        {historyState.status === "ready" && historyState.items.length === 0 && (
          <EmptyMessage>
            You haven&apos;t submitted any correction requests yet. Requests you submit will appear here.
          </EmptyMessage>
        )}
        {historyState.status === "ready" && historyState.items.length > 0 && (
          <div className="ws-table-wrap">
            <table className="ws-table">
              <caption className="ws-visually-hidden">
                Your treatment correction requests, newest first
              </caption>
              <thead>
                <tr>
                  <th scope="col">Request ID</th>
                  <th scope="col">Submitted</th>
                  <th scope="col">Current status</th>
                  <th scope="col">Details</th>
                </tr>
              </thead>
              <tbody>
                {historyState.items.map((item) => {
                  const approval = describeApprovalStatus(item);
                  const decision = describeDecision(item.guardrail_decision);
                  return (
                    <tr key={item.request_id}>
                      <th scope="row">#{item.request_id}</th>
                      <td>
                        {item.created_at && formatDateTime(item.created_at) ? (
                          <time dateTime={item.created_at}>{formatDateTime(item.created_at)}</time>
                        ) : "—"}
                      </td>
                      <td><Badge tone={approval.tone}>{approval.label}</Badge></td>
                      <td>
                        <details>
                          <summary className="ws-link-button">View details</summary>
                          <dl className="ws-details">
                            <div>
                              <dt>Request type</dt>
                              <dd>Treatment correction</dd>
                            </div>
                            <div>
                              <dt>Guardrail decision</dt>
                              <dd><Badge tone={decision.tone}>{decision.label}</Badge></dd>
                            </div>
                            <div>
                              <dt>Physician review</dt>
                              <dd>
                                <Badge tone={approval.tone}>{approval.label}</Badge>
                                {" "}{approval.description}
                              </dd>
                            </div>
                            <div>
                              <dt>Reviewed</dt>
                              <dd>
                                {item.reviewed_at
                                  ? formatDateTime(item.reviewed_at) ?? "Date unavailable"
                                  : "Not yet reviewed"}
                              </dd>
                            </div>
                          </dl>
                        </details>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}
