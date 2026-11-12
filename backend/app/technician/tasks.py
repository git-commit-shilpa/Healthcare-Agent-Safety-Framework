"""Technician task reads (and, below, writes through the database functions).

Every query is scoped by the caller's verified user id AND their live patient
assignments, because the backend uses the service-role key and RLS does not apply to it.
Unassigned and unknown tasks are indistinguishable (404 "Task not found.").
"""

from typing import Any

from app.main import CurrentUser, _insert_chat_audit, _patient_service_rows
from app.scheduling import service as scheduling
from app.scheduling.features import appointments_enabled, require_appointments_enabled
from app.technician import correction as correction_flow
from app.technician import gateway, service
from app.technician.catalogue import (
    ALL_STATUSES,
    DECISION_FROM_STATUSES,
    DEFAULT_ALLOW_PATIENT_MESSAGE,
    DEFAULT_COMPLETED_PATIENT_MESSAGE,
    CORRECTION_CATEGORY,
)
from app.technician.errors import TaskApiError
from app.technician.rpc import call_rpc
from app.technician.schemas import (
    AppointmentInfo,
    ApprovalSummary,
    CompleteRequest,
    CorrectionActionRequest,
    CorrectionActionResponse,
    DecisionRequest,
    DecisionResponse,
    PatientRef,
    SimpleStatusResponse,
    SlotCheck,
    TaskDetail,
    TaskEvent,
    TaskList,
    TaskSummary,
    TechnicianDashboard,
)

TASKS_UNAVAILABLE = "Tasks are temporarily unavailable."
TASK_NOT_FOUND = "Task not found."
_TASK_COLUMNS = (
    "task_id,patient_id,requested_by,category,title,description,status,latest_decision,"
    "patient_message,requires_physician_approval,approval_request_id,created_at,updated_at"
)
_EVENT_COLUMNS = (
    "event_id,actor_user_id,actor_role,event_type,decision,hold_reason,from_status,to_status,"
    "guardrail_decision,guardrail_reason,internal_note,patient_message,requester_note,created_at"
)
_APPROVAL_STATE = {"Pending": "pending", "Approved": "approved", "Rejected": "rejected"}


def _in(ids: list[int]) -> str:
    return f"in.({','.join(str(int(i)) for i in ids)})"


async def _rows(table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    return await _patient_service_rows(table, params, unavailable_detail=TASKS_UNAVAILABLE)


async def approval_states(request_ids: list[int]) -> dict[int, str]:
    """Coarse approval state for display and for the gateway.

    The database function re-validates strictly (patient, task, action, reviewer role,
    assignment, permission) before anything executes; this only reads approvals.status.
    """
    clean = sorted({i for i in request_ids if isinstance(i, int) and not isinstance(i, bool)})
    if not clean:
        return {}
    rows = await _rows("approvals", {"select": "request_id,status", "request_id": _in(clean)})
    grouped: dict[int, list[str]] = {}
    for row in rows:
        grouped.setdefault(row.get("request_id"), []).append(row.get("status"))
    states: dict[int, str] = {}
    for request_id in clean:
        statuses = grouped.get(request_id, [])
        # "missing" (no approval row at all) is only meaningful for a Technician-first correction; _approval_for decides.
        states[request_id] = (
            _APPROVAL_STATE.get(statuses[0], "invalid") if len(statuses) == 1
            else "missing" if not statuses else "invalid"
        )
    return states


def _approval_for(row: dict[str, Any], approvals: dict[int, str]) -> str:
    request_id = row.get("approval_request_id")
    if request_id is None:
        return "none"
    state = approvals.get(request_id, "invalid")
    if state == "missing":
        return "not_requested" if row.get("category") == CORRECTION_CATEGORY else "invalid"
    return state


def _summary(row: dict[str, Any], names: dict[int, str], approvals: dict[int, str]) -> TaskSummary:
    return TaskSummary(
        task_id=row["task_id"],
        patient=PatientRef(patient_id=row["patient_id"], name=names.get(row.get("requested_by"))),
        category=row["category"],
        title=row["title"],
        status=row["status"],
        latest_decision=row.get("latest_decision"),
        requires_physician_approval=row["requires_physician_approval"],
        approval_status=_approval_for(row, approvals),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


async def _own_task_rows(
    user: CurrentUser,
    *,
    status: str | None = None,
    task_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    columns: str = _TASK_COLUMNS,
) -> list[dict[str, Any]]:
    patient_ids = await service._assigned_patient_ids(user)
    if not patient_ids:
        return []
    params = {
        "select": columns,
        "assigned_to": f"eq.{int(user.user_id)}",
        "patient_id": _in(patient_ids),
        "order": "created_at.desc,task_id.desc",
        "limit": str(int(limit)),
        "offset": str(int(offset)),
    }
    if status is not None:
        params["status"] = f"eq.{status}"
    if task_id is not None:
        params["task_id"] = f"eq.{int(task_id)}"
    return await _rows("technician_tasks", params)


async def _decorate(rows: list[dict[str, Any]]) -> list[TaskSummary]:
    names = await service._patient_names([r.get("requested_by") for r in rows])
    approvals = await approval_states([r["approval_request_id"] for r in rows if r.get("approval_request_id")])
    return [_summary(r, names, approvals) for r in rows]


async def list_tasks(
    user: CurrentUser, *, status: str | None, limit: int, offset: int
) -> TaskList:
    if status is not None and status not in ALL_STATUSES:
        raise TaskApiError(422, "Unknown status filter.", "invalid_request")
    rows = await _own_task_rows(user, status=status, limit=limit, offset=offset)
    return TaskList(items=await _decorate(rows), limit=limit, offset=offset)


async def load_own_task(user: CurrentUser, task_id: int) -> dict[str, Any]:
    """The caller's task row, or 404. Used by reads and before every write."""
    rows = await _own_task_rows(user, task_id=task_id, limit=1)
    if not rows:
        await _insert_chat_audit(user, "technician.task.access_denied", {"task_id": int(task_id)})
        raise TaskApiError(404, TASK_NOT_FOUND, "not_found")
    return rows[0]


APPOINTMENT_CATEGORY = "appointment_scheduling"
_APPOINTMENT_COLUMNS = (
    "appointment_id,task_id,physician_user_id,starts_at,ends_at,physician_timezone,reason,status"
)
# Minimum patient-facing text (mirrors the database trigger in migration 013).
MIN_REJECTION_CHARS = 20
MIN_QUESTION_CHARS = 10
MIN_CONFIRMATION_CHARS = 10


async def load_appointment(task_id: int) -> dict[str, Any] | None:
    """Only call AFTER load_own_task has proven the task is the caller's.

    Returns None while the appointments feature is OFF (the table may not exist yet), so tasks
    in the appointment_scheduling category keep behaving exactly like any other task.
    """
    if not appointments_enabled():
        return None
    rows = await _rows("appointments", {"select": _APPOINTMENT_COLUMNS, "task_id": f"eq.{int(task_id)}"})
    rows = [r for r in rows if r.get("task_id") == int(task_id)]
    return rows[0] if rows else None


async def _physician_name(physician_id: int | None) -> str | None:
    if not isinstance(physician_id, int):
        return None
    rows = await _rows("users", {"select": "user_id,name", "user_id": f"eq.{physician_id}"})
    return next((r["name"] for r in rows if r.get("user_id") == physician_id and isinstance(r.get("name"), str)), None)


def _appointment_info(
    apt: dict[str, Any], physician_name: str | None, events: list[dict[str, Any]]
) -> AppointmentInfo:
    zone = apt.get("physician_timezone") or "UTC"
    replies = [
        e.get("requester_note") for e in events
        if e.get("event_type") == "info_provided" and e.get("requester_note")
    ]
    return AppointmentInfo(
        appointment_id=apt["appointment_id"],
        physician_id=apt.get("physician_user_id"),
        physician_name=physician_name,
        starts_at=scheduling.utc_iso(apt["starts_at"]),
        ends_at=scheduling.utc_iso(apt["ends_at"]),
        timezone=zone,
        local_label=scheduling.local_label(apt["starts_at"], zone),
        reason=apt.get("reason"),
        status=apt["status"],
        status_label=scheduling.status_label(apt["status"]),
        patient_response=replies[-1] if replies else None,
    )


async def get_task(user: CurrentUser, task_id: int) -> TaskDetail:
    row = await load_own_task(user, task_id)
    summary = (await _decorate([row]))[0]
    events = await _rows(
        "technician_task_events",
        {"select": _EVENT_COLUMNS, "task_id": f"eq.{int(task_id)}", "order": "created_at.asc,event_id.asc"},
    )
    appointment = None
    if row["category"] == APPOINTMENT_CATEGORY:
        apt = await load_appointment(task_id)  # None while the feature is off or for legacy tasks
        if apt is not None:
            appointment = _appointment_info(apt, await _physician_name(apt.get("physician_user_id")), events)
    correction = await correction_flow.build_info(row) if correction_flow.is_correction(row) else None
    detail = TaskDetail(
        **summary.model_dump(),
        description=row.get("description"),
        patient_message=row.get("patient_message"),
        appointment=appointment,
        correction=correction,
        events=[
            TaskEvent(
                **{k: e.get(k) for k in (
                    "event_id", "event_type", "actor_role", "decision", "hold_reason", "from_status",
                    "to_status", "guardrail_decision", "guardrail_reason", "internal_note",
                    "patient_message", "requester_note", "created_at")},
                by_me=e.get("actor_user_id") == user.user_id,
            )
            for e in events
        ],
    )
    await _insert_chat_audit(user, "technician.task.viewed", {"task_id": int(task_id)})
    return detail


async def list_approvals(user: CurrentUser) -> list[ApprovalSummary]:
    rows = [r for r in await _own_task_rows(user, limit=200) if r.get("approval_request_id") is not None]
    summaries = [s for s in await _decorate(rows) if s.approval_status != "not_requested"]
    return [
        ApprovalSummary(
            task_id=s.task_id, patient=s.patient, category=s.category,
            task_status=s.status, approval_status=s.approval_status,
        )
        for s in summaries
    ]


async def dashboard(user: CurrentUser) -> TechnicianDashboard:
    patient_ids = await service._assigned_patient_ids(user)
    rows = await _own_task_rows(user, limit=500) if patient_ids else []
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    recent = sorted(rows, key=lambda r: (r.get("updated_at") or "", r["task_id"]), reverse=True)[:5]
    return TechnicianDashboard(
        assigned_patients=len(patient_ids),
        open_tasks=sum(n for s, n in counts.items() if s not in ("Completed", "Declined", "Cancelled")),
        pending_review=counts.get("Submitted", 0) + counts.get("In Review", 0),
        in_progress=counts.get("In Progress", 0),
        on_hold=counts.get("On Hold", 0),
        awaiting_physician=counts.get("Awaiting Physician", 0),
        completed=counts.get("Completed", 0),
        recent_tasks=await _decorate(recent),
    )


# ---- Writes: every state change goes through a database function ----------------------------

INVALID_STATE = "That action is not available for the request in its current state."


def _require(result: dict[str, Any], *keys: str) -> dict[str, Any]:
    if any(k not in result for k in keys):
        raise TaskApiError(502, "Service requests are temporarily unavailable.", "unavailable")
    return result


async def _approval_status_of(row: dict[str, Any]) -> str:
    request_id = row.get("approval_request_id")
    if request_id is None:
        return "none"
    return _approval_for(row, await approval_states([request_id]))


async def start_review(user: CurrentUser, task_id: int) -> SimpleStatusResponse:
    row = await load_own_task(user, task_id)
    if row["status"] not in ("Submitted", "On Hold"):
        raise TaskApiError(409, INVALID_STATE, "invalid_state")
    result = _require(
        await call_rpc(
            "start_technician_task_review",
            {"p_task_id": int(task_id), "p_actor_user_id": int(user.user_id)},
        ),
        "task_id", "status",
    )
    return SimpleStatusResponse(task_id=result["task_id"], status=result["status"])


async def _appointment_patient_message(
    task_id: int, body: DecisionRequest, message: str | None, guardrail_decision: str
) -> str | None:
    """Context-specific wording rules for appointment decisions (the database enforces them too)."""
    if body.decision == "ALLOW":
        if message is None and guardrail_decision == "Allow":
            apt = await load_appointment(task_id)
            if apt is not None:
                name = await _physician_name(apt.get("physician_user_id")) or "your doctor"
                label = scheduling.local_label(apt["starts_at"], apt.get("physician_timezone"))
                return f"Your appointment with {name} on {label} is confirmed."
            return DEFAULT_ALLOW_PATIENT_MESSAGE
        if message is not None and len(message) < MIN_CONFIRMATION_CHARS:
            raise TaskApiError(
                422, "Please write a short confirmation message for the patient.",
                "confirmation_message_required")
    elif body.decision == "BLOCK" and len(message or "") < MIN_REJECTION_CHARS:
        raise TaskApiError(
            422, "Please explain the rejection to the patient (at least 20 characters).",
            "rejection_reason_too_short")
    elif (
        body.decision == "HOLD" and body.hold_reason == "needs_information"
        and len(message or "") < MIN_QUESTION_CHARS
    ):
        raise TaskApiError(
            422, "Please tell the patient what information is needed (at least 10 characters).",
            "information_request_too_short")
    return message


def _appointment_explanation(body: DecisionRequest, outcome: str, fallback: str) -> str:
    if body.decision == "ALLOW" and outcome == "executed":
        return "Appointment confirmed. The time slot is now reserved and the patient can see your confirmation message."
    if body.decision == "BLOCK" and outcome == "recorded":
        return "Request rejected. The patient can see your explanation in their portal."
    if body.decision == "HOLD" and body.hold_reason == "needs_information" and outcome == "recorded":
        return "Question sent to the patient. Review resumes when they reply."
    return fallback


_SLOT_MESSAGES = {
    "ok": "The doctor is available at this time.",
    "already_confirmed": "This appointment is already confirmed.",
    "slot_unavailable": "This time is no longer available. Reject the request or ask the patient to choose another time.",
    "physician_unavailable": "The doctor is not currently available for appointments.",
    "not_pending": "This request is not waiting for confirmation.",
}


async def check_appointment_slot(user: CurrentUser, task_id: int) -> SlotCheck:
    require_appointments_enabled()
    row = await load_own_task(user, task_id)
    if row["category"] != APPOINTMENT_CATEGORY:
        raise TaskApiError(404, TASK_NOT_FOUND, "not_found")
    result = _require(
        await call_rpc(
            "check_appointment_slot",
            {"p_task_id": int(task_id), "p_actor_user_id": int(user.user_id)},
        ),
        "task_id", "result", "bookable",
    )
    if result["result"] not in _SLOT_MESSAGES:
        raise TaskApiError(502, "Service requests are temporarily unavailable.", "unavailable")
    return SlotCheck(
        task_id=result["task_id"], result=result["result"], bookable=bool(result["bookable"]),
        message=_SLOT_MESSAGES[result["result"]],
    )


async def decide(user: CurrentUser, task_id: int, body: DecisionRequest) -> DecisionResponse:
    """Technician chooses; the backend gateway decides; the database re-verifies and records."""
    try:
        body.validate_for_decision()
    except ValueError as error:
        code = "note_required" if "require" in str(error) else "invalid_request"
        raise TaskApiError(422, str(error), code) from error

    row = await load_own_task(user, task_id)  # 404 unless mine and still assigned
    if correction_flow.is_correction(row):
        # A Technician coordinates corrections; only the Physician approves or rejects them (also enforced by the database).
        raise correction_flow.refuse_clinical_action()
    if row["status"] not in DECISION_FROM_STATUSES[body.decision]:
        raise TaskApiError(409, INVALID_STATE, "invalid_state")

    approval_status = await _approval_status_of(row)
    if body.decision == "BLOCK" and row["status"] == "Awaiting Physician" and approval_status == "pending":
        raise TaskApiError(409, "A physician approval is still pending for this request.", "approval_pending")

    # Facts below come from the database, never from the request body.
    verdict = gateway.evaluate(
        permissions=user.permissions,
        decision=body.decision,
        hold_reason=body.hold_reason,
        category=row["category"],
        requires_physician_approval=bool(row["requires_physician_approval"]),
        approval_status=approval_status,
        current_status=row["status"],
    )
    if verdict.decision == "Block" and body.decision != "ALLOW":
        raise TaskApiError(403, verdict.explanation, verdict.reason)

    patient_message = body.patient_message
    # Appointment rules apply only to tasks that really have an appointments row (never to legacy
    # appointment_scheduling tasks created before migration 013, nor while the feature is off).
    is_appointment = row["category"] == APPOINTMENT_CATEGORY and await load_appointment(task_id) is not None
    if is_appointment:
        patient_message = await _appointment_patient_message(task_id, body, patient_message, verdict.decision)
    elif patient_message is None and body.decision == "ALLOW" and verdict.decision == "Allow":
        patient_message = DEFAULT_ALLOW_PATIENT_MESSAGE

    try:
        result = _require(
            await call_rpc(
                "record_technician_task_decision",
                {
                    "p_task_id": int(task_id),
                    "p_actor_user_id": int(user.user_id),
                    "p_decision": body.decision,
                    "p_hold_reason": body.hold_reason,
                    "p_internal_note": body.internal_note,
                    "p_patient_message": patient_message,
                    "p_guardrail_decision": verdict.decision,
                    "p_guardrail_reason": verdict.reason,
                },
            ),
            "task_id", "status", "outcome",
        )
    except TaskApiError as error:
        if is_appointment and error.code == "slot_unavailable":
            # The whole transaction rolled back, so record the conflict separately (ids only).
            await _insert_chat_audit(user, "technician.appointment.confirm_conflict", {"task_id": int(task_id)})
        raise
    raw_approval = result.get("approval_status")
    return DecisionResponse(
        outcome=result["outcome"],
        guardrail_decision=verdict.decision,
        guardrail_reason=verdict.reason,
        explanation=(
            _appointment_explanation(body, result["outcome"], verdict.explanation)
            if is_appointment else verdict.explanation
        ),
        task_id=result["task_id"],
        status=result["status"],
        latest_decision=result.get("latest_decision"),
        approval_status=raw_approval.lower() if isinstance(raw_approval, str) else None,
    )


async def complete(user: CurrentUser, task_id: int, body: CompleteRequest) -> SimpleStatusResponse:
    row = await load_own_task(user, task_id)
    if correction_flow.is_correction(row):
        raise correction_flow.refuse_clinical_action()
    if row["status"] != "In Progress":
        raise TaskApiError(409, INVALID_STATE, "invalid_state")
    if row["requires_physician_approval"] and await _approval_status_of(row) != "approved":
        raise TaskApiError(409, "Physician approval is required before this request can be completed.", "approval_required")
    message = (body.patient_message or "").strip() or DEFAULT_COMPLETED_PATIENT_MESSAGE
    result = _require(
        await call_rpc(
            "complete_technician_task",
            {
                "p_task_id": int(task_id),
                "p_actor_user_id": int(user.user_id),
                "p_patient_message": message,
            },
        ),
        "task_id", "status",
    )
    return SimpleStatusResponse(task_id=result["task_id"], status=result["status"])


async def coordinate_correction(
    user: CurrentUser, task_id: int, body: CorrectionActionRequest
) -> CorrectionActionResponse:
    row = await load_own_task(user, task_id)  # 404 unless mine and still assigned
    return await correction_flow.coordinate(user, row, body)
