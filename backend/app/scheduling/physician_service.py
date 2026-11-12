"""Physician schedule management. The physician id ALWAYS comes from the verified user.

Every write goes through a database function that re-checks the actor (active Physician with
the manage_own_schedule permission) and that the row belongs to them. Reads are filtered by the
caller's id in code because the backend uses the service-role key.
"""

from typing import Any

from app.main import CurrentUser, _patient_service_rows
from app.scheduling import service as scheduling
from app.scheduling.physician_schemas import (
    PhysicianAppointment,
    SavedResponse,
    ScheduleExceptionIn,
    ScheduleExceptionOut,
    ScheduleRuleIn,
    ScheduleRuleOut,
    minutes_to_hhmm,
)
from app.technician.errors import TaskApiError
from app.technician.rpc import call_rpc

UNAVAILABLE = "Your schedule is temporarily unavailable."


def _need(result: dict[str, Any], *keys: str) -> dict[str, Any]:
    if any(k not in result for k in keys):
        raise TaskApiError(502, UNAVAILABLE, "unavailable")
    return result


async def list_rules(user: CurrentUser) -> list[ScheduleRuleOut]:
    rows = await _patient_service_rows(
        "physician_schedule_rules",
        {
            "select": "rule_id,physician_user_id,weekday,start_minute,end_minute,slot_minutes,timezone,"
                      "effective_from,effective_to,is_active",
            "physician_user_id": f"eq.{int(user.user_id)}",
            "order": "weekday.asc,start_minute.asc",
        },
        unavailable_detail=UNAVAILABLE,
    )
    return [
        ScheduleRuleOut(
            rule_id=r["rule_id"], weekday=r["weekday"],
            start_time=minutes_to_hhmm(r["start_minute"]), end_time=minutes_to_hhmm(r["end_minute"]),
            slot_minutes=r["slot_minutes"], timezone=r["timezone"],
            effective_from=r.get("effective_from"), effective_to=r.get("effective_to"),
            is_active=bool(r["is_active"]),
        )
        for r in rows
        if r.get("physician_user_id") == int(user.user_id)  # defence in depth
    ]


async def save_rule(user: CurrentUser, body: ScheduleRuleIn) -> SavedResponse:
    result = _need(
        await call_rpc(
            "upsert_schedule_rule",
            {
                "p_actor_user_id": int(user.user_id),
                "p_rule_id": body.rule_id,
                "p_weekday": body.weekday,
                "p_start_minute": body.start_minute,
                "p_end_minute": body.end_minute,
                "p_slot_minutes": body.slot_minutes,
                "p_timezone": body.timezone,
                "p_effective_from": body.effective_from.isoformat() if body.effective_from else None,
                "p_effective_to": body.effective_to.isoformat() if body.effective_to else None,
            },
        ),
        "rule_id",
    )
    return SavedResponse(id=result["rule_id"])


async def deactivate_rule(user: CurrentUser, rule_id: int) -> SavedResponse:
    result = _need(
        await call_rpc(
            "deactivate_schedule_rule", {"p_actor_user_id": int(user.user_id), "p_rule_id": int(rule_id)}
        ),
        "rule_id",
    )
    return SavedResponse(id=result["rule_id"])


async def list_exceptions(user: CurrentUser) -> list[ScheduleExceptionOut]:
    rows = await _patient_service_rows(
        "physician_schedule_exceptions",
        {
            "select": "exception_id,physician_user_id,kind,starts_at,ends_at,slot_minutes,reason,is_active",
            "physician_user_id": f"eq.{int(user.user_id)}",
            "is_active": "eq.true",
            "order": "starts_at.asc",
        },
        unavailable_detail=UNAVAILABLE,
    )
    return [
        ScheduleExceptionOut(
            exception_id=r["exception_id"], kind=r["kind"],
            starts_at=scheduling.utc_iso(r["starts_at"]), ends_at=scheduling.utc_iso(r["ends_at"]),
            slot_minutes=r.get("slot_minutes"), reason=r.get("reason"),
        )
        for r in rows
        if r.get("physician_user_id") == int(user.user_id) and r.get("is_active") is True
    ]


async def add_exception(user: CurrentUser, body: ScheduleExceptionIn) -> SavedResponse:
    result = _need(
        await call_rpc(
            "add_schedule_exception",
            {
                "p_actor_user_id": int(user.user_id),
                "p_kind": body.kind,
                "p_starts_at": scheduling.utc_iso(body.starts_at),
                "p_ends_at": scheduling.utc_iso(body.ends_at),
                "p_slot_minutes": body.slot_minutes,
                "p_reason": body.reason,
            },
        ),
        "exception_id",
    )
    return SavedResponse(id=result["exception_id"])


async def remove_exception(user: CurrentUser, exception_id: int) -> SavedResponse:
    result = _need(
        await call_rpc(
            "remove_schedule_exception",
            {"p_actor_user_id": int(user.user_id), "p_exception_id": int(exception_id)},
        ),
        "exception_id",
    )
    return SavedResponse(id=result["exception_id"])


async def list_appointments(user: CurrentUser, limit: int = 100) -> list[PhysicianAppointment]:
    """The Physician's OWN appointments. Patient name only: no reason text, no clinical data."""
    rows = await _patient_service_rows(
        "appointments",
        {
            "select": "appointment_id,task_id,patient_id,physician_user_id,starts_at,ends_at,physician_timezone,status",
            "physician_user_id": f"eq.{int(user.user_id)}",
            "order": "starts_at.desc",
            "limit": str(int(limit)),
        },
        unavailable_detail=UNAVAILABLE,
    )
    rows = [r for r in rows if r.get("physician_user_id") == int(user.user_id)]
    names: dict[int, str] = {}
    patient_ids = sorted({r["patient_id"] for r in rows if isinstance(r.get("patient_id"), int)})
    if patient_ids:
        patients = await _patient_service_rows(
            "patients",
            {"select": "patient_id,user_id", "patient_id": f"in.({','.join(str(i) for i in patient_ids)})"},
            unavailable_detail=UNAVAILABLE,
        )
        user_ids = sorted({p["user_id"] for p in patients if isinstance(p.get("user_id"), int)})
        users = (
            await _patient_service_rows(
                "users",
                {"select": "user_id,name", "user_id": f"in.({','.join(str(i) for i in user_ids)})"},
                unavailable_detail=UNAVAILABLE,
            )
            if user_ids else []
        )
        by_user = {u["user_id"]: u["name"] for u in users if isinstance(u.get("name"), str)}
        names = {p["patient_id"]: by_user.get(p.get("user_id")) for p in patients}
    return [
        PhysicianAppointment(
            appointment_id=r["appointment_id"], request_id=r["task_id"],
            patient_name=names.get(r.get("patient_id")),
            starts_at=scheduling.utc_iso(r["starts_at"]), ends_at=scheduling.utc_iso(r["ends_at"]),
            local_label=scheduling.local_label(r["starts_at"], r.get("physician_timezone")),
            status=r["status"],
        )
        for r in rows
    ]
