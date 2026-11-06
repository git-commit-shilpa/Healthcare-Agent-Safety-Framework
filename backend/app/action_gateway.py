"""Deterministic Action Gateway for the Patient AI assistant (dry run only).

The language model may only *propose* an intent. Whether that intent is
allowed, blocked or escalated is decided here from the authenticated user's
role and permissions and a fixed risk table. Nothing in this module executes
an action or touches the database.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

Decision = Literal["Allow", "Block", "Escalate"]
Risk = Literal["low", "high"]

MAX_MODEL_OUTPUT_CHARS = 500
ASSISTANT_PERMISSION = "request_ai_assistance"
UNKNOWN_INTENT = "unknown"


@dataclass(frozen=True)
class ActionSpec:
    permission: str
    risk: Risk


# Fixed allowlist. There is deliberately no action that updates a medical record.
ACTION_CATALOGUE: dict[str, ActionSpec] = {
    "explain_process": ActionSpec(permission=ASSISTANT_PERMISSION, risk="low"),
    "view_own_record": ActionSpec(permission="view_own_record", risk="low"),
    "view_history": ActionSpec(permission="view_own_correction_requests", risk="low"),
    "request_correction": ActionSpec(permission="submit_record_correction", risk="high"),
    # Educational answers only: no record data is read and nothing is executed.
    "general_health_info": ActionSpec(permission=ASSISTANT_PERMISSION, risk="low"),
    "symptom_guidance": ActionSpec(permission=ASSISTANT_PERMISSION, risk="low"),
}

# Intents answered by the model in educational mode (general information + disclaimer).
HEALTH_INTENTS = frozenset({"general_health_info", "symptom_guidance"})

# Plain-language reason for every decision code (shown to the patient and written to the audit log).
REASON_EXPLANATIONS: dict[str, str] = {
    "low_risk_permitted": "This is a low-risk informational request that your role is allowed to make.",
    "high_risk_requires_physician": "Changes to your record need review by your assigned Physician, so this was escalated.",
    "greeting": "A simple greeting or question about what the assistant can do.",
    "no_pending_step": "There was no pending step waiting for a yes or no.",
    "guardrail_blocked": "The safety checks stopped this request, so no answer was generated.",
    "invalid_model_output": "The request could not be understood safely, so it was not acted on.",
    "unknown_intent": "The request did not match a supported topic, so only a scope-limited answer is given.",
    "permission_missing": "Your account does not have the permission this request needs.",
    "role_not_permitted": "This assistant is only available to patient accounts.",
    "record_retrieved": "You are allowed to view your own record, so the saved details you asked about were read for you.",
    "record_not_found": "You are allowed to view your own record, but no medical record is linked to your account yet.",
    "record_unavailable": "You are allowed to view your own record, but it could not be retrieved right now. Nothing was shown.",
    "response_blocked_by_safety_check": "The request was permitted, but the safety check stopped the generated answer, so nothing was shared.",
    "emergency_detected": "The message describes a possible emergency, so you are directed to emergency help instead of a chat answer.",
    "other_patient_access": "Requests for another person's information are never allowed.",
    "prescription_not_permitted": "The assistant cannot prescribe or recommend medication; a Physician must do that.",
    "approval_bypass_not_permitted": "Approvals and Physician review cannot be skipped or granted from chat; only your assigned Physician can approve a correction.",
    "dosage_not_permitted": "The assistant cannot say how much medicine to take; a Physician or pharmacist must advise you.",
    "diagnosis_not_permitted": "The assistant cannot diagnose; a Physician must assess your personal situation.",
    "direct_record_change": "Records cannot be changed from chat; a physician-reviewed correction request is required.",
}


REASON_EXPLANATIONS.update({
    "response_blocked_by_input_rail": "The request was permitted, but the input safety check stopped it before an answer was generated, so nothing was shared.",
    "response_blocked_by_output_rail": "The request was permitted, but the model-based output safety check stopped the generated answer, so nothing was shared.",
    "response_blocked_by_output_check": "The request was permitted, but the automatic output check stopped the generated answer, so nothing was shared.",
})
_OUTPUT_CHECK_PREFIX = "response_blocked_by_output_check_"
_OUTPUT_CHECK_CATEGORIES = {
    "personal_diagnosis": "it appeared to diagnose you",
    "clinician_claim": "it appeared to speak as a clinician",
    "dose_or_prescription": "it contained a dose or prescription",
    "personalised_treatment": "it appeared to change your treatment",
    "record_change_claim": "it claimed to change a record or request",
    "approval_bypass": "it suggested skipping your Physician",
    "privacy": "it contained personal details",
    "instruction_leak": "it referred to hidden instructions",
}


def explain(reason_code: str) -> str:
    """Human-readable explanation for a decision code; unknown codes fail to a safe generic text."""
    if reason_code.startswith(_OUTPUT_CHECK_PREFIX):
        why = _OUTPUT_CHECK_CATEGORIES.get(reason_code[len(_OUTPUT_CHECK_PREFIX):])
        if why:
            return (
                "The request was permitted, but the automatic output check stopped the generated "
                f"answer because {why}, so nothing was shared."
            )
        return REASON_EXPLANATIONS["response_blocked_by_output_check"]
    return REASON_EXPLANATIONS.get(reason_code, REASON_EXPLANATIONS["guardrail_blocked"])


@dataclass(frozen=True)
class IntentProposal:
    intent: str
    confidence: float


@dataclass(frozen=True)
class GatewayDecision:
    intent: str
    decision: Decision
    reason_code: str


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_proposal(raw: Any) -> IntentProposal | None:
    """Strictly parse model output. Returns None for anything unexpected.

    Only the ``intent`` and ``confidence`` keys are read; any other key the
    model emits (for example a claimed permission) is ignored.
    """
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_MODEL_OUTPUT_CHARS:
        return None
    try:
        data = json.loads(_FENCE.sub("", raw.strip()))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    confidence = data.get("confidence")
    if not isinstance(intent, str) or isinstance(confidence, bool):
        return None
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return None
    if intent != UNKNOWN_INTENT and intent not in ACTION_CATALOGUE:
        return None
    return IntentProposal(intent=intent, confidence=float(confidence))


def decide(
    *,
    role: str,
    permissions: list[str],
    proposal: IntentProposal | None,
    rails_passed: bool,
) -> GatewayDecision:
    """Return Allow, Block or Escalate. Order matters; every gap fails closed."""
    if role != "patient":
        return GatewayDecision(UNKNOWN_INTENT, "Block", "role_not_permitted")
    if not rails_passed:
        return GatewayDecision(UNKNOWN_INTENT, "Block", "guardrail_blocked")
    if proposal is None:
        return GatewayDecision(UNKNOWN_INTENT, "Block", "invalid_model_output")
    spec = ACTION_CATALOGUE.get(proposal.intent)
    if spec is None:
        return GatewayDecision(UNKNOWN_INTENT, "Block", "unknown_intent")
    # The assistant permission is required for every proposal, plus the action's own.
    if ASSISTANT_PERMISSION not in permissions or spec.permission not in permissions:
        return GatewayDecision(proposal.intent, "Block", "permission_missing")
    if spec.risk == "high":
        return GatewayDecision(proposal.intent, "Escalate", "high_risk_requires_physician")
    return GatewayDecision(proposal.intent, "Allow", "low_risk_permitted")
