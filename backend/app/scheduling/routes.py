from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.main import CurrentUser
from app.scheduling import service
from app.scheduling.permissions import (
    REQUEST_APPOINTMENT,
    VIEW_OWN_SERVICE_REQUESTS,
    VIEW_PHYSICIAN_AVAILABILITY,
    require_patient_appointment_permission,
)
from app.scheduling.schemas import (
    AppointmentCreate,
    AppointmentDetail,
    AppointmentItem,
    AppointmentReply,
    AvailabilityResponse,
    PhysicianOption,
    StatusResponse,
    SubmitAppointmentResponse,
)

router = APIRouter(prefix="/api/patient/appointments", tags=["patient-appointments"])

CanBrowse = Annotated[
    CurrentUser, Depends(require_patient_appointment_permission(VIEW_PHYSICIAN_AVAILABILITY))
]
CanRequest = Annotated[CurrentUser, Depends(require_patient_appointment_permission(REQUEST_APPOINTMENT))]
CanView = Annotated[CurrentUser, Depends(require_patient_appointment_permission(VIEW_OWN_SERVICE_REQUESTS))]


@router.get("/physicians", response_model=list[PhysicianOption])
async def list_physicians(user: CanBrowse) -> list[PhysicianOption]:
    return await service.physicians()


@router.get("/physicians/{physician_id}/availability", response_model=AvailabilityResponse)
async def physician_availability(
    physician_id: int, user: CanBrowse, day: Annotated[date, Query(alias="date")]
) -> AvailabilityResponse:
    return await service.availability(physician_id, day)


@router.get("", response_model=list[AppointmentItem])
async def list_my_appointments(
    user: CanView, limit: Annotated[int, Query(ge=1, le=100)] = 100
) -> list[AppointmentItem]:
    return await service.list_appointments(user, limit)


@router.post("", response_model=SubmitAppointmentResponse, status_code=201)
async def request_appointment(body: AppointmentCreate, user: CanRequest) -> SubmitAppointmentResponse:
    return await service.submit(user, body)


@router.get("/{task_id}", response_model=AppointmentDetail)
async def appointment_detail(task_id: int, user: CanView) -> AppointmentDetail:
    return await service.detail(user, task_id)


@router.post("/{task_id}/cancel", response_model=StatusResponse)
async def cancel_appointment(task_id: int, user: CanRequest) -> StatusResponse:
    return await service.cancel(user, task_id)


@router.post("/{task_id}/respond", response_model=StatusResponse)
async def respond_to_question(task_id: int, body: AppointmentReply, user: CanRequest) -> StatusResponse:
    return await service.respond(user, task_id, body)
