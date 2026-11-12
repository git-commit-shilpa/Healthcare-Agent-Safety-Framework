"""Patient appointment logic.

The requester id always comes from the verified user. Availability is read from the database
on every call (never cached, never produced by a model). Showing a slot books nothing; the
only write is submit_appointment_request, which creates a PENDING request. The slot is reserved
later, atomically, when a Technician confirms (see migration 013).

Nothing here talks to an AI model, and free text is never written to audit rows.
"""

from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import safety_rules
from app.main import CurrentUser, _insert_chat_audit, _patient_service_rows
from app.scheduling.schemas import (
    AppointmentCreate,
    AppointmentDetail,
    AppointmentItem,
    AppointmentReply,
    AvailabilityResponse,
    PhysicianOption,
    SlotOption,
    StatusResponse,
    SubmitAppointmentResponse,
    TimelineEntry,
)
from app.service_requests import service as service_requests
from app.technician.errors import TaskApiError
from app.technician.rpc import call_rpc

UNAVAILABLE = "Appointments are temporarily unavailable."
CATEGORY = "appointment_scheduling"

STATUS_LABELS = {
    "Pending Confirmation": "Pending Technician Confirmation",
    "Needs Information": "More information needed",
    "Awaiting Physician": "Awaiting physician review",
    "Confirmed": "Confirmed",
    "Rejected": "Not approved",
    "Cancelled": "Cancelled",
    "Completed": "Completed",
}
_CANCELLABLE = ("Pending Confirmation", "Needs Information")
CONFIRM_REQUIRED = "Please review the summary and confirm before sending your request."
REASON_NOT_ACCEPTED = (
    'Please keep the reason short and non-clinical (for example, "annual check-up"). '
    "Your care team will talk through medical details with you."
)
SUBMITTED_MESSAGE = (
    "Your request has been sent and is waiting for a Technician to confirm it. "
    "This time is NOT booked yet; we will update this page once it is confirmed."
)

_APPT_COLUMNS = "appointment_id,task_id,physician_user_id,starts_at,ends_at,physician_timezone,reason,status"
_TASK_COLUMNS = "task_id,status,patient_message,created_at"


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


# -- formatting -----------------------------------------------------------------------------
def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def utc_iso(value: str | datetime) -> str:
    moment = value if isinstance(value, datetime) else _parse(value)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_label(value: str | datetime, zone_name: str | None) -> str:
    """'Mon, 17 Nov 2026, 9:00 AM EST' in the doctor's time zone (falls back to UTC)."""
    moment = value if isinstance(value, datetime) else _parse(value)
    try:
        zone = ZoneInfo(zone_name) if zone_name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    local = moment.astimezone(zone)
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a, %d %b %Y')}, {hour}:{local.strftime('%M %p %Z')}"


def group_for(status: str, ends_at: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if status in ("Pending Confirmation", "Needs Information", "Awaiting Physician"):
        return "pending"
    if status == "Confirmed":
        return "upcoming" if _parse(ends_at) > now else "completed"
    if status == "Completed":
        return "completed"
    return "rejected"  # Rejected / Cancelled


# -- guards -----------------------------------------------------------------------------------
async def _screen_reason(user: CurrentUser, text: str | None) -> None:
    """Reuse the deterministic safety screen. A short administrative reason is all we accept."""
    if not text:
        return
    verdict = safety_rules.screen(text)
    if verdict is None:
        return
    # Reason code only: never the patient's text.
    await _insert_chat_audit(user, "patient.appointment.not_accepted", {"reason": verdict.reason_code})
    if verdict.intent == "emergency":
        raise TaskApiError(422, verdict.reply, "emergency_detected")
    raise TaskApiError(422, REASON_NOT_ACCEPTED, "reason_not_accepted")


def _need(result: dict[str, Any], *keys: str) -> dict[str, Any]:
    if any(k not in result for k in keys):
        raise TaskApiError(502, UNAVAILABLE, "unavailable")
    return result


async def _own_appointment_task(user: CurrentUser, task_id: int) -> dict[str, Any]:
    """The caller's own appointment request, or 404 (other people's ids are indistinguishable)."""
    rows = await _patient_service_rows(
        "technician_tasks",
        {
            "select": _TASK_COLUMNS,
            "task_id": f"eq.{int(task_id)}",
            "requested_by": f"eq.{int(user.user_id)}",
            "category": f"eq.{CATEGORY}",
        },
        unavailable_detail=UNAVAILABLE,
    )
    rows = [r for r in rows if r.get("task_id") == int(task_id)]
    if not rows:
        raise TaskApiError(404, "Not found.", "not_found")
    return rows[0]


# -- doctors and availability ---------------------------------------------------------------------
async def physicians() -> list[PhysicianOption]:
    result = _need(await call_rpc("list_bookable_physicians", {}), "physicians")
    out: list[PhysicianOption] = []
    for p in result["physicians"]:
        if isinstance(p, dict) and isinstance(p.get("user_id"), int) and isinstance(p.get("name"), str):
            out.append(PhysicianOption(physician_id=p["user_id"], name=p["name"], timezone=p.get("timezone")))
    return out


async def availability(physician_id: int, day: date) -> AvailabilityResponse:
    result = _need(
        await call_rpc(
            "list_physician_availability",
            {"p_physician_id": int(physician_id), "p_from": day.isoformat(), "p_to": day.isoformat()},
        ),
        "slots",
    )
    zone = result.get("timezone")
    slots = [
        SlotOption(start=utc_iso(s["start"]), end=utc_iso(s["end"]), local_label=local_label(s["start"], zone))
        for s in result["slots"]
        if isinstance(s, dict) and s.get("start") and s.get("end")
    ]
    return AvailabilityResponse(physician_id=int(physician_id), timezone=zone, date=day.isoformat(), slots=slots)


# -- requests ---------------------------------------------------------------------------------------
async def submit(user: CurrentUser, body: AppointmentCreate) -> SubmitAppointmentResponse:
    if body.confirmed is not True:
        raise TaskApiError(422, CONFIRM_REQUIRED, "confirmation_required")
    await _screen_reason(user, body.reason)
    result = _need(
        await call_rpc(
            "submit_appointment_request",
            {
                "p_requested_by": int(user.user_id),  # never taken from the request body
                "p_physician_id": body.physician_id,
                "p_starts_at": utc_iso(body.starts_at),
                "p_reason": body.reason,
                "p_guardrail_reason": "low_risk_permitted",
            },
        ),
        "task_id", "appointment_id", "status", "starts_at", "ends_at", "timezone",
    )
    return SubmitAppointmentResponse(
        request_id=result["task_id"],
        appointment_id=result["appointment_id"],
        status=result["status"],
        status_label=status_label(result["status"]),
        starts_at=utc_iso(result["starts_at"]),
        ends_at=utc_iso(result["ends_at"]),
        timezone=result["timezone"],
        booked=False,
        message=SUBMITTED_MESSAGE,
    )


async def _load_items(user: CurrentUser, limit: int, task_id: int | None = None) -> list[AppointmentItem]:
    params = {
        "select": _TASK_COLUMNS,
        "requested_by": f"eq.{int(user.user_id)}",  # only ever the verified caller
        "category": f"eq.{CATEGORY}",
        "order": "created_at.desc,task_id.desc",
        "limit": str(int(limit)),
    }
    if task_id is not None:
        params["task_id"] = f"eq.{int(task_id)}"
    tasks = {
        r["task_id"]: r
        for r in await _patient_service_rows("technician_tasks", params, unavailable_detail=UNAVAILABLE)
        if isinstance(r.get("task_id"), int)
    }
    if not tasks:
        return []
    ids = ",".join(str(i) for i in sorted(tasks))
    appointments = await _patient_service_rows(
        "appointments",
        {"select": _APPT_COLUMNS, "task_id": f"in.({ids})"},
        unavailable_detail=UNAVAILABLE,
    )
    appointments = [a for a in appointments if a.get("task_id") in tasks]  # defence in depth
    physician_ids = sorted(
        {a["physician_user_id"] for a in appointments if isinstance(a.get("physician_user_id"), int)}
    )
    names: dict[int, str] = {}
    if physician_ids:
        rows = await _patient_service_rows(
            "users",
            {"select": "user_id,name", "user_id": f"in.({','.join(str(i) for i in physician_ids)})"},
            unavailable_detail=UNAVAILABLE,
        )
        names = {r["user_id"]: r["name"] for r in rows if isinstance(r.get("name"), str)}

    items: list[AppointmentItem] = []
    for a in appointments:
        task = tasks[a["task_id"]]
        status = a["status"]
        zone = a.get("physician_timezone")
        items.append(
            AppointmentItem(
                request_id=a["task_id"],
                appointment_id=a["appointment_id"],
                physician_name=names.get(a.get("physician_user_id")),
                starts_at=utc_iso(a["starts_at"]),
                ends_at=utc_iso(a["ends_at"]),
                timezone=zone or "UTC",
                local_label=local_label(a["starts_at"], zone),
                reason=a.get("reason"),
                status=status,
                status_label=status_label(status),
                group=group_for(status, a["ends_at"]),
                patient_message=task.get("patient_message"),
                can_cancel=status in _CANCELLABLE,
                can_add_info=status == "Needs Information",
                created_at=task.get("created_at"),
            )
        )
    items.sort(key=lambda i: (i.created_at or "", i.request_id), reverse=True)
    return items


async def list_appointments(user: CurrentUser, limit: int = 100) -> list[AppointmentItem]:
    return await _load_items(user, limit)


def _timeline_entry(event: dict[str, Any]) -> TimelineEntry | None:
    """Patient-visible history only. Internal notes and guardrail fields are never selected."""
    kind = event.get("event_type")
    at = event.get("created_at")
    message = event.get("patient_message")
    if kind == "created":
        return TimelineEntry(at=at, kind="submitted", title="Request sent")
    if kind == "decision":
        decision, hold = event.get("decision"), event.get("hold_reason")
        if decision == "ALLOW":
            return TimelineEntry(at=at, kind="confirmed", title="Appointment confirmed", text=message)
        if decision == "BLOCK":
            return TimelineEntry(at=at, kind="rejected", title="Request not approved", text=message)
        if hold == "needs_information":
            return TimelineEntry(at=at, kind="info_requested", title="More information requested", text=message)
        if hold == "physician_approval":
            return TimelineEntry(at=at, kind="physician_review", title="Sent for physician review", text=message)
    if kind == "info_provided":
        return TimelineEntry(at=at, kind="your_reply", title="You replied", text=event.get("requester_note"))
    if kind == "completed":
        return TimelineEntry(at=at, kind="completed", title="Appointment completed", text=message)
    if kind == "cancelled":
        return TimelineEntry(at=at, kind="cancelled", title="You cancelled this request")
    return None  # review_started, decision_rejected, approval_requested, ... are internal


async def detail(user: CurrentUser, task_id: int) -> AppointmentDetail:
    await _own_appointment_task(user, task_id)
    items = await _load_items(user, 1, task_id=task_id)
    if not items:
        raise TaskApiError(404, "Not found.", "not_found")
    events = await _patient_service_rows(
        "technician_task_events",
        {
            # internal_note and the guardrail columns are deliberately NOT selected.
            "select": "event_id,event_type,decision,hold_reason,patient_message,requester_note,created_at",
            "task_id": f"eq.{int(task_id)}",
            "order": "event_id.asc",
        },
        unavailable_detail=UNAVAILABLE,
    )
    timeline = [t for t in (_timeline_entry(e) for e in events) if t is not None]
    return AppointmentDetail(**items[0].model_dump(), timeline=timeline)


async def cancel(user: CurrentUser, task_id: int) -> StatusResponse:
    await _own_appointment_task(user, task_id)
    result = await service_requests.cancel(user, task_id)
    return StatusResponse(request_id=result.task_id, status="Cancelled", status_label=status_label("Cancelled"))


async def respond(user: CurrentUser, task_id: int, body: AppointmentReply) -> StatusResponse:
    await _own_appointment_task(user, task_id)
    await service_requests.add_info(user, task_id, body)  # also runs the safety screen
    return StatusResponse(
        request_id=int(task_id), status="Pending Confirmation", status_label=status_label("Pending Confirmation")
    )
