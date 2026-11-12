from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.main import CurrentUser
from app.technician import service, tasks
from app.technician.permissions import (
    TECHNICIAN_ACTIONS,
    UPDATE_TASK_STATUS,
    VIEW_ASSIGNED_PATIENT,
    VIEW_ASSIGNED_TASK,
    require_task_permission,
    require_technician,
    require_technician_permission,
)
from app.technician.schemas import (
    ApprovalSummary,
    AssignedPatient,
    CompleteRequest,
    CorrectionActionRequest,
    CorrectionActionResponse,
    DecisionRequest,
    DecisionResponse,
    SimpleStatusResponse,
    SlotCheck,
    TaskDetail,
    TaskList,
    TechnicianAccount,
    TechnicianDashboard,
)

router = APIRouter(prefix="/api/technician", tags=["technician"])

CanViewTasks = Annotated[CurrentUser, Depends(require_task_permission(VIEW_ASSIGNED_TASK))]
CanUpdateTasks = Annotated[CurrentUser, Depends(require_task_permission(UPDATE_TASK_STATUS))]


@router.get("/me", response_model=TechnicianAccount)
async def get_technician_account(
    user: Annotated[CurrentUser, Depends(require_technician)],
) -> TechnicianAccount:
    return TechnicianAccount(
        user_id=user.user_id,
        email=user.email,
        role=user.role.value,
        permissions=sorted(p for p in user.permissions if p in TECHNICIAN_ACTIONS),
    )


@router.get("/patients", response_model=list[AssignedPatient])
async def list_my_patients(
    user: Annotated[
        CurrentUser, Depends(require_technician_permission(VIEW_ASSIGNED_PATIENT))
    ],
) -> list[AssignedPatient]:
    return await service.list_assigned_patients(user)


@router.get("/patients/{patient_id}", response_model=AssignedPatient)
async def get_my_patient(
    patient_id: int,
    user: Annotated[
        CurrentUser, Depends(require_technician_permission(VIEW_ASSIGNED_PATIENT))
    ],
) -> AssignedPatient:
    return await service.get_assigned_patient(user, patient_id)


# ---- Tasks (feature-gated: HASF_SERVICE_REQUESTS_ENABLED) ------------------------------------


@router.get("/dashboard", response_model=TechnicianDashboard)
async def get_dashboard(user: CanViewTasks) -> TechnicianDashboard:
    return await tasks.dashboard(user)


@router.get("/tasks", response_model=TaskList)
async def list_my_tasks(
    user: CanViewTasks,
    status: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> TaskList:
    return await tasks.list_tasks(user, status=status, limit=limit, offset=offset)


@router.get("/tasks/{task_id}", response_model=TaskDetail)
async def get_my_task(task_id: int, user: CanViewTasks) -> TaskDetail:
    return await tasks.get_task(user, task_id)


@router.get("/tasks/{task_id}/appointment-check", response_model=SlotCheck)
async def check_appointment(task_id: int, user: CanViewTasks) -> SlotCheck:
    """Read-only: is the requested slot still bookable? Reserves nothing."""
    return await tasks.check_appointment_slot(user, task_id)


@router.get("/approvals", response_model=list[ApprovalSummary])
async def list_my_approvals(user: CanViewTasks) -> list[ApprovalSummary]:
    return await tasks.list_approvals(user)


@router.post("/tasks/{task_id}/start-review", response_model=SimpleStatusResponse)
async def start_task_review(task_id: int, user: CanUpdateTasks) -> SimpleStatusResponse:
    return await tasks.start_review(user, task_id)


@router.post("/tasks/{task_id}/decision", response_model=DecisionResponse)
async def decide_task(task_id: int, body: DecisionRequest, user: CanUpdateTasks) -> DecisionResponse:
    return await tasks.decide(user, task_id, body)


@router.post("/tasks/{task_id}/correction-action", response_model=CorrectionActionResponse)
async def coordinate_correction_task(
    task_id: int, body: CorrectionActionRequest, user: CanUpdateTasks
) -> CorrectionActionResponse:
    """Coordination only: ready for Physician review, ask the Patient, close after the Physician decided."""
    return await tasks.coordinate_correction(user, task_id, body)


@router.post("/tasks/{task_id}/complete", response_model=SimpleStatusResponse)
async def complete_task(
    task_id: int, user: CanUpdateTasks, body: CompleteRequest | None = None
) -> SimpleStatusResponse:
    return await tasks.complete(user, task_id, body or CompleteRequest())
