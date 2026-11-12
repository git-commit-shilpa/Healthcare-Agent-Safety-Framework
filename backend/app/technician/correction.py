"""Technician-first handling of Patient medical-record correction requests (migrations 014 and 015).

Flow: Patient -> Technician -> HOLD / DENY. A correction is one ai_requests row plus one Technician task and NO
Physician approval. The assigned Technician can:
  * HOLD for information         (request_information)
  * HOLD for physician approval  (request_physician_authorization): the database creates the one approval for the unique
                                 assigned Physician, and only if that Physician can actually process it
  * DENY for a standard ADMINISTRATIVE reason, and only before any approval exists (decline)
  * close the coordination task once the Physician has finally approved or rejected (close)
There is no ALLOW: only the Physician authorizes a clinical change, and a Technician can never override that decision.

Privacy: this module reads ids, statuses and timestamps only. It never selects ``request_details`` (the correction
text) and never returns the reviewing Physician's identity. Patient replies are never sent to any model.
"""

from typing import Any

from app.main import CurrentUser, _insert_chat_audit, _patient_service_rows
from app.technician import gateway
from app.technician.catalogue import CORRECTION_CATEGORY, DECLINE_REASONS, TERMINAL_STATUSES
from app.technician.errors import TaskApiError
from app.technician.rpc import call_rpc
from app.technician.schemas import (
    CorrectionActionRequest,
    CorrectionActionResponse,
    CorrectionInfo,
    DeclineReason,
)

UNAVAILABLE = "Tasks are temporarily unavailable."
USE_COORDINATION = (
    "Correction requests are coordinated by the care team and decided by the Physician. "
    "Use the correction actions on this request instead."
)
_APPROVAL_STATE = {"Pending": "pending", "Approved": "approved", "Rejected": "rejected"}
_FINAL = ("approved", "rejected")

_NEXT_STEP_MESSAGES = {
    "request_physician_authorization": "Sent to the assigned Physician for authorization. The patient sees that it is with their Physician.",
    "request_information": "Question sent to the Patient. Review resumes when they reply.",
    "decline": "Request declined for an administrative reason. The patient can see your explanation.",
    "close": "Coordination task closed. The Physician's decision is unchanged and remains visible to the Patient.",
}
_WHY_NOT = {
    "already_decided": "The Physician has already decided this correction.",
    "approval_pending": "The Physician has not finally decided this correction yet.",
    "cannot_decline_after_authorization": "This correction is already with the Physician, so it cannot be declined.",
}
# What the gateway is asked about each action (it then confirms the Technician may do it).
_GATEWAY_QUESTION = {
    "request_physician_authorization": ("HOLD", "physician_approval"),
    "request_information": ("HOLD", "needs_information"),
    "decline": ("BLOCK", None),
}


def is_correction(row: dict[str, Any]) -> bool:
    return row.get("category") == CORRECTION_CATEGORY


async def read_approval(row: dict[str, Any]) -> tuple[str, str | None]:
    """(state, reviewed_at). The database re-validates strictly before any write; this is for display.

    State is not_requested | pending | approved | rejected | invalid. Reads statuses and ids only.
    """
    request_id = row.get("approval_request_id")
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        return "invalid", None
    approvals = await _patient_service_rows(
        "approvals",
        {"select": "request_id,status,reviewed_at", "request_id": f"eq.{request_id}"},
        unavailable_detail=UNAVAILABLE,
    )
    approvals = [a for a in approvals if a.get("request_id") == request_id]
    requests = await _patient_service_rows(
        "ai_requests",
        {"select": "request_id,patient_id,requested_by,action", "request_id": f"eq.{request_id}"},
        unavailable_detail=UNAVAILABLE,
    )
    request = next((r for r in requests if r.get("request_id") == request_id), None)
    if (
        request is None
        or request.get("action") != "patient_treatment_correction"
        or request.get("patient_id") != row.get("patient_id")
        or request.get("requested_by") != row.get("requested_by")
    ):
        return "invalid", None
    if not approvals:
        return "not_requested", None
    if len(approvals) != 1:
        return "invalid", None
    state = _APPROVAL_STATE.get(approvals[0].get("status"), "invalid")
    reviewed = approvals[0].get("reviewed_at")
    return state, reviewed if isinstance(reviewed, str) else None


def allowed_actions(status: str, approval_state: str) -> list[str]:
    actions: list[str] = []
    if approval_state in ("not_requested", "pending"):
        if status == "In Review":
            actions.append("request_physician_authorization")
        if status in ("In Review", "Awaiting Physician"):
            actions.append("request_information")
    if approval_state == "not_requested" and status in ("In Review", "On Hold"):
        actions.append("decline")
    if approval_state in _FINAL and status not in TERMINAL_STATUSES:
        actions.append("close")
    return actions


async def build_info(row: dict[str, Any]) -> CorrectionInfo:
    state, reviewed_at = await read_approval(row)
    actions = allowed_actions(row["status"], state)
    return CorrectionInfo(
        request_id=row["approval_request_id"],
        approval_status=state,
        reviewed_at=reviewed_at,
        allowed_actions=actions,
        decline_reasons=[DeclineReason(code=c, label=l) for c, l in DECLINE_REASONS.items()] if "decline" in actions else [],
    )


def refuse_clinical_action() -> TaskApiError:
    return TaskApiError(409, USE_COORDINATION, "use_correction_coordination")


def _db_action(body: CorrectionActionRequest) -> str:
    if body.action == "ready_for_review":          # 014 name, kept as an alias
        return "request_physician_authorization"
    if body.action == "decline":
        return f"decline_{body.reason_code}"
    return body.action


async def coordinate(user: CurrentUser, row: dict[str, Any], body: CorrectionActionRequest) -> CorrectionActionResponse:
    if not is_correction(row):
        raise TaskApiError(409, "This is not a correction request.", "not_a_correction_task")
    action = "request_physician_authorization" if body.action == "ready_for_review" else body.action
    state, _ = await read_approval(row)

    if body.action == "decline":
        if body.reason_code not in DECLINE_REASONS:
            raise TaskApiError(422, "Choose one of the standard administrative reasons.", "invalid_reason")
        if len(body.patient_message or "") < 20:
            raise TaskApiError(422, "Please explain the reason to the patient (at least 20 characters).", "rejection_reason_too_short")
    elif body.reason_code is not None:
        raise TaskApiError(422, "A reason code applies only to declining.", "invalid_request")
    if action in ("request_physician_authorization", "request_information", "decline") and not body.internal_note:
        raise TaskApiError(422, "An internal note is required.", "note_required")
    if action == "request_information" and len(body.patient_message or "") < 10:
        raise TaskApiError(
            422, "Please tell the patient what information is needed (at least 10 characters).",
            "information_request_too_short")

    if action not in allowed_actions(row["status"], state):
        if state in _FINAL and action != "close":
            raise TaskApiError(409, _WHY_NOT["already_decided"], "already_decided")
        if action == "decline" and state == "pending":
            raise TaskApiError(409, _WHY_NOT["cannot_decline_after_authorization"], "cannot_decline_after_authorization")
        if action == "close" and state in ("pending", "not_requested"):
            raise TaskApiError(409, _WHY_NOT["approval_pending"], "approval_pending")
        if state == "invalid":
            raise TaskApiError(409, "The linked approval is not valid for this request.", "approval_invalid")
        raise TaskApiError(409, "That action is not available for the request in its current state.", "invalid_state")

    # The independent backend gateway (same one the generic Technician decisions use). A correction is always clinical,
    # so it can only ever Escalate or be declined administratively; a Block verdict stops here.
    question = _GATEWAY_QUESTION.get(action)
    if question is not None:
        verdict = gateway.evaluate(
            permissions=user.permissions, decision=question[0], hold_reason=question[1], category=row["category"],
            requires_physician_approval=True, approval_status="none" if state == "not_requested" else state,  # type: ignore[arg-type]
            current_status=row["status"],
        )
        if verdict.decision == "Block":
            raise TaskApiError(403, verdict.explanation, verdict.reason)

    try:
        result = await call_rpc(
            "coordinate_correction_task",
            {
                "p_task_id": int(row["task_id"]),
                "p_actor_user_id": int(user.user_id),
                "p_action": _db_action(body),
                "p_internal_note": body.internal_note,
                "p_patient_message": body.patient_message,
            },
        )
    except TaskApiError as error:
        if error.code == "physician_unreachable":
            # Ids only: which task could not be escalated and why. Never text.
            await _insert_chat_audit(user, "technician.correction.escalation_blocked",
                                     {"task_id": int(row["task_id"]), "reason": "physician_unreachable"})
        raise
    if any(k not in result for k in ("task_id", "status", "action")):
        raise TaskApiError(502, UNAVAILABLE, "unavailable")
    return CorrectionActionResponse(
        task_id=result["task_id"], status=result["status"], action=body.action,
        outcome=result.get("outcome") if isinstance(result.get("outcome"), str) else None,
        message=_NEXT_STEP_MESSAGES[action],
    )
