"""Patient service-request logic. The requester id always comes from the verified user."""

from typing import Any

from app import safety_rules
from app.main import CurrentUser, _insert_chat_audit, _patient_service_rows
from app.service_requests.schemas import (
    CategoryInfo,
    ServiceRequestCreate,
    ServiceRequestInfo,
    ServiceRequestItem,
    StatusResponse,
    SubmitResponse,
)
from app.scheduling.features import appointments_enabled
from app.technician.catalogue import ALL_CATEGORIES, CLINICAL_CATEGORIES, CORRECTION_CATEGORY, is_clinical
from app.technician.errors import TaskApiError
from app.technician.rpc import call_rpc

UNAVAILABLE = "Your service requests are temporarily unavailable."
_COLUMNS = (
    "task_id,category,title,description,status,patient_message,"
    "requires_physician_approval,created_at,updated_at"
)
_CANCELLABLE = ("Submitted", "In Review", "On Hold")

USE_CLINICAL_CATEGORY = (
    "This sounds like a clinical question. Please choose the medication, treatment or "
    "diagnosis category so a physician can review it."
)
USE_RECORD_CORRECTION = (
    "To correct your medical record, please use the record correction request instead."
)


# When HASF_APPOINTMENTS_ENABLED is on, appointments have their own flow (doctor, date, time slot)
# under /api/patient/appointments. While it is OFF, the generic category behaves exactly as before.
APPOINTMENT_CATEGORY = "appointment_scheduling"
USE_APPOINTMENTS = "Please use the Appointments page to request an appointment."


def categories() -> list[CategoryInfo]:
    return [
        CategoryInfo(value=value, label=label, requires_physician_review=value in CLINICAL_CATEGORIES)
        for value, label in ALL_CATEGORIES.items()
        if not (value == APPOINTMENT_CATEGORY and appointments_enabled())
    ]


async def _block(user: CurrentUser, code: str, detail: str, reason: str) -> TaskApiError:
    # Reason code only: never the patient's text.
    await _insert_chat_audit(user, "patient.service_request.not_accepted", {"reason": reason})
    return TaskApiError(422, detail, code)


async def screen_text(user: CurrentUser, category: str, *texts: str | None) -> None:
    """Reuse the existing deterministic safety screen on free text before anything is stored.

    Emergencies are never queued. Clinical wording is fine for the clinical categories (a
    Physician reviews those) but is refused for routine ones so it cannot skip review.
    """
    verdict = safety_rules.screen(". ".join(t for t in texts if t))
    if verdict is None:
        return
    if verdict.intent == "emergency":
        raise await _block(user, "emergency_detected", verdict.reply, verdict.reason_code)
    if verdict.intent in ("other_patient_access", "approval_bypass"):
        raise await _block(user, "request_not_permitted", verdict.reply, verdict.reason_code)
    if verdict.intent in ("prescription_request", "dosage_request", "diagnosis_request"):
        if not is_clinical(category):
            raise await _block(user, "use_clinical_category", USE_CLINICAL_CATEGORY, verdict.reason_code)
        return
    if verdict.intent == "record_change_request":
        raise await _block(user, "use_record_correction", USE_RECORD_CORRECTION, verdict.reason_code)


def _int_result(result: dict[str, Any], *keys: str) -> dict[str, Any]:
    if any(k not in result for k in keys):
        raise TaskApiError(502, UNAVAILABLE, "unavailable")
    return result


async def submit(user: CurrentUser, body: ServiceRequestCreate) -> SubmitResponse:
    if body.category == APPOINTMENT_CATEGORY and appointments_enabled():
        raise TaskApiError(422, USE_APPOINTMENTS, "use_appointments")
    await screen_text(user, body.category, body.title, body.description)
    reason = "clinical_requires_physician" if is_clinical(body.category) else "low_risk_permitted"
    result = _int_result(
        await call_rpc(
            "submit_service_request",
            {
                "p_requested_by": int(user.user_id),
                "p_category": body.category,
                "p_title": body.title,
                "p_description": body.description,
                "p_guardrail_reason": reason,
            },
        ),
        "task_id", "status", "requires_physician_approval",
    )
    return SubmitResponse(
        task_id=result["task_id"],
        status=result["status"],
        requires_physician_review=bool(result["requires_physician_approval"]),
    )


async def list_requests(user: CurrentUser, limit: int = 50) -> list[ServiceRequestItem]:
    rows = await _patient_service_rows(
        "technician_tasks",
        {
            "select": _COLUMNS,
            "requested_by": f"eq.{int(user.user_id)}",  # only ever the verified caller
            # Corrections appear (once) in the correction list with their coordination status, not here.
            "category": f"neq.{CORRECTION_CATEGORY}",
            "order": "created_at.desc,task_id.desc",
            "limit": str(int(limit)),
        },
        unavailable_detail=UNAVAILABLE,
    )
    return [
        ServiceRequestItem(
            task_id=r["task_id"],
            category=r["category"],
            category_label=ALL_CATEGORIES.get(r["category"], r["category"]),
            title=r["title"],
            description=r.get("description"),
            status=r["status"],
            patient_message=r.get("patient_message"),
            requires_physician_review=bool(r["requires_physician_approval"]),
            can_cancel=r["status"] in _CANCELLABLE,
            can_add_info=r["status"] == "On Hold",
            created_at=r.get("created_at"),
            updated_at=r.get("updated_at"),
        )
        for r in rows
    ]


async def cancel(user: CurrentUser, task_id: int) -> StatusResponse:
    if await _is_correction_task(user, task_id):
        raise TaskApiError(
            409, "A correction request cannot be cancelled here. Your Physician reviews it.", "not_cancellable")
    result = _int_result(
        await call_rpc(
            "cancel_service_request",
            {"p_task_id": int(task_id), "p_requested_by": int(user.user_id)},
        ),
        "task_id", "status",
    )
    return StatusResponse(task_id=result["task_id"], status=result["status"])


async def _is_correction_task(user: CurrentUser, task_id: int) -> bool:
    rows = await _patient_service_rows(
        "technician_tasks",
        {"select": "task_id,category", "task_id": f"eq.{int(task_id)}", "requested_by": f"eq.{int(user.user_id)}"},
        unavailable_detail=UNAVAILABLE,
    )
    return any(r.get("task_id") == int(task_id) and r.get("category") == CORRECTION_CATEGORY for r in rows)


async def add_info(user: CurrentUser, task_id: int, body: ServiceRequestInfo) -> StatusResponse:
    if len(body.message) > MAX_CORRECTION_REPLY and await _is_correction_task(user, task_id):
        raise TaskApiError(
            422, f"Please keep your reply under {MAX_CORRECTION_REPLY} characters and leave out medical details.",
            "reply_too_long")
    await screen_text(user, "general_inquiry", body.message)
    result = _int_result(
        await call_rpc(
            "add_service_request_info",
            {"p_task_id": int(task_id), "p_requested_by": int(user.user_id), "p_message": body.message},
        ),
        "task_id", "status",
    )
    return StatusResponse(task_id=result["task_id"], status=result["status"])


# ---- Record-correction coordination, as the Patient may see it (migration 014) --------------------

# Patient-facing wording for each coordination task status.
_CORRECTION_STATUS_LABELS = {
    "Submitted": "Received by your care team",
    "In Review": "Being checked by your care team",
    "On Hold": "Your care team needs more information from you",
    "Awaiting Physician": "With your Physician for review.",
    "Completed": "Care team review finished",
    "Declined": "Closed by your care team",
}
MAX_CORRECTION_REPLY = 500  # stricter than the general 1000: a reply is not the place for correction text


async def correction_coordination(user_id: int, request_ids: list[int]) -> dict[int, Any]:
    """{correction request_id: coordination} for the caller's OWN corrections. Read-only. No notes, no staff."""
    from app.main import PatientCorrectionCoordination  # late import: app.main imports this module lazily

    if not request_ids:
        return {}
    rows = await _patient_service_rows(
        "technician_tasks",
        {
            "select": "task_id,status,patient_message,approval_request_id",
            "requested_by": f"eq.{int(user_id)}",  # only ever the verified caller
            "category": f"eq.{CORRECTION_CATEGORY}",
            "approval_request_id": f"in.({','.join(str(int(i)) for i in request_ids)})",
        },
        unavailable_detail=UNAVAILABLE,
    )
    out: dict[int, Any] = {}
    for row in rows:
        request_id = row.get("approval_request_id")
        if request_id not in request_ids or request_id in out:
            continue
        status = row.get("status")
        out[request_id] = PatientCorrectionCoordination(
            task_id=row["task_id"],
            status=status,
            status_label=_CORRECTION_STATUS_LABELS.get(status, "With your care team"),
            patient_message=row.get("patient_message"),
            can_add_info=status == "On Hold",
        )
    return out
