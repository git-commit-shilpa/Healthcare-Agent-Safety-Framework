"""Physician schedule routes (NEW; the existing Physician module is not touched).

Physician accounts cannot sign in to the app yet (no auth link), so these routes are only
reachable once that exists. They are disabled unless the appointment feature flag is on AND the
Physician / manage_own_schedule permission row has been set to allowed = true (the Admin
portal's permissions page is read-only, so this is a deliberate SQL change by the database owner).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.main import CurrentUser, Role, get_current_user
from app.scheduling import physician_service as service
from app.scheduling.features import require_appointments_enabled
from app.scheduling.physician_schemas import (
    PhysicianAppointment,
    SavedResponse,
    ScheduleExceptionIn,
    ScheduleExceptionOut,
    ScheduleRuleIn,
    ScheduleRuleOut,
)
from fastapi import HTTPException

MANAGE_OWN_SCHEDULE = "manage_own_schedule"

router = APIRouter(prefix="/api/physician/schedule", tags=["physician-schedule"])


async def require_schedule_manager(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Order: authenticated -> Physician role -> feature enabled -> permission row."""
    if user.role != Role.PHYSICIAN:
        raise HTTPException(status_code=403, detail="Physician role is required.")
    require_appointments_enabled()
    if MANAGE_OWN_SCHEDULE not in user.permissions:
        raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
    return user


Manager = Annotated[CurrentUser, Depends(require_schedule_manager)]


@router.get("/rules", response_model=list[ScheduleRuleOut])
async def list_my_rules(user: Manager) -> list[ScheduleRuleOut]:
    return await service.list_rules(user)


@router.post("/rules", response_model=SavedResponse, status_code=201)
async def save_my_rule(body: ScheduleRuleIn, user: Manager) -> SavedResponse:
    return await service.save_rule(user, body)


@router.post("/rules/{rule_id}/deactivate", response_model=SavedResponse)
async def deactivate_my_rule(rule_id: int, user: Manager) -> SavedResponse:
    return await service.deactivate_rule(user, rule_id)


@router.get("/exceptions", response_model=list[ScheduleExceptionOut])
async def list_my_exceptions(user: Manager) -> list[ScheduleExceptionOut]:
    return await service.list_exceptions(user)


@router.post("/exceptions", response_model=SavedResponse, status_code=201)
async def add_my_exception(body: ScheduleExceptionIn, user: Manager) -> SavedResponse:
    return await service.add_exception(user, body)


@router.post("/exceptions/{exception_id}/remove", response_model=SavedResponse)
async def remove_my_exception(exception_id: int, user: Manager) -> SavedResponse:
    return await service.remove_exception(user, exception_id)


@router.get("/appointments", response_model=list[PhysicianAppointment])
async def list_my_appointments(
    user: Manager, limit: Annotated[int, Query(ge=1, le=200)] = 100
) -> list[PhysicianAppointment]:
    return await service.list_appointments(user, limit)
