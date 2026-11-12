"""Technician task gateway: an independent backend verdict on a Technician's choice.

The Technician only *chooses* ALLOW / BLOCK / HOLD. This module decides, from facts
the backend loaded itself (never from the request body), whether that choice may take
effect: Allow, Block or Escalate. The database function re-verifies the same
invariants again inside one transaction, so a bug here cannot bypass an approval.

Deterministic and fail-closed; no model is involved and no free text is read.
"""

from dataclasses import dataclass
from typing import Literal

from app.technician.catalogue import is_clinical

Verdict = Literal["Allow", "Block", "Escalate"]
ApprovalStatus = Literal["none", "pending", "approved", "rejected", "invalid"]


@dataclass(frozen=True)
class GatewayResult:
    decision: Verdict
    reason: str  # short machine code, stored in the event log
    explanation: str  # plain language for the Technician


EXPLANATIONS = {
    "permission_missing": "Your role does not have permission to update requests.",
    "invalid_decision": "That decision is not recognised.",
    "routine_permitted": "This is a routine request that you may process.",
    "approved_by_physician": "A Physician has approved this request, so you may proceed.",
    "physician_approval_required": (
        "This request needs a Physician's approval first. Use HOLD and choose physician approval."
    ),
    "approval_rejected": "The Physician rejected this request. It can only be declined.",
    "approval_invalid": "The linked approval is not valid for this request, so it cannot proceed.",
    "decline_permitted": "You may decline this request.",
    "hold_for_information": "The request will be held until more information is provided.",
    "physician_approval_requested": "A Physician approval request will be created for this request.",
}


def _result(decision: Verdict, reason: str) -> GatewayResult:
    return GatewayResult(decision, reason, EXPLANATIONS[reason])


def evaluate(
    *,
    permissions: list[str],
    decision: str,
    hold_reason: str | None,
    category: str,
    requires_physician_approval: bool,
    approval_status: ApprovalStatus,
    current_status: str,
) -> GatewayResult:
    if "update_task_status" not in permissions:
        return _result("Block", "permission_missing")

    if decision == "ALLOW":
        # A task already held for physician approval needs it even if the category is routine.
        if (
            requires_physician_approval
            or is_clinical(category)
            or current_status == "Awaiting Physician"
        ):
            if approval_status == "approved":
                return _result("Allow", "approved_by_physician")
            if approval_status in ("none", "pending"):
                return _result("Escalate", "physician_approval_required")
            if approval_status == "rejected":
                return _result("Block", "approval_rejected")
            return _result("Block", "approval_invalid")
        return _result("Allow", "routine_permitted")

    if decision == "BLOCK":
        return _result("Allow", "decline_permitted")

    if decision == "HOLD":
        if hold_reason == "needs_information":
            return _result("Allow", "hold_for_information")
        if hold_reason == "physician_approval":
            return _result("Escalate", "physician_approval_requested")

    return _result("Block", "invalid_decision")
