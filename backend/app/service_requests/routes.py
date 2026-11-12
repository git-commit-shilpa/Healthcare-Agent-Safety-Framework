from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.main import CurrentUser
from app.service_requests import service
from app.service_requests.permissions import (
    SUBMIT_SERVICE_REQUEST,
    VIEW_OWN_SERVICE_REQUESTS,
    require_patient_permission,
)
from app.service_requests.schemas import (
    CategoryInfo,
    ServiceRequestCreate,
    ServiceRequestInfo,
    ServiceRequestItem,
    StatusResponse,
    SubmitResponse,
)

router = APIRouter(prefix="/api/patient/service-requests", tags=["patient-service-requests"])

CanSubmit = Annotated[CurrentUser, Depends(require_patient_permission(SUBMIT_SERVICE_REQUEST))]
CanView = Annotated[CurrentUser, Depends(require_patient_permission(VIEW_OWN_SERVICE_REQUESTS))]


@router.get("/categories", response_model=list[CategoryInfo])
async def list_categories(user: CanSubmit) -> list[CategoryInfo]:
    return service.categories()


@router.get("", response_model=list[ServiceRequestItem])
async def list_my_requests(
    user: CanView, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[ServiceRequestItem]:
    return await service.list_requests(user, limit)


@router.post("", response_model=SubmitResponse, status_code=201)
async def submit_request(body: ServiceRequestCreate, user: CanSubmit) -> SubmitResponse:
    return await service.submit(user, body)


@router.post("/{task_id}/cancel", response_model=StatusResponse)
async def cancel_request(task_id: int, user: CanSubmit) -> StatusResponse:
    return await service.cancel(user, task_id)


@router.post("/{task_id}/info", response_model=StatusResponse)
async def add_request_info(task_id: int, body: ServiceRequestInfo, user: CanSubmit) -> StatusResponse:
    return await service.add_info(user, task_id, body)
