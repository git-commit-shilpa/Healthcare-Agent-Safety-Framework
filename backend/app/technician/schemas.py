from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TechnicianAccount(BaseModel):
    user_id: int
    email: str
    role: str
    permissions: list[str]


class AssignedPatient(BaseModel):
    """Read-only patient summary for an ASSIGNED technician. Name, but never email or user_id."""

    patient_id: int
    name: str | None = None
    age: int | None = None
    medical_condition: str | None = None
    treatment: str | None = None


# ---- Tasks -------------------------------------------------------------------------------

ApprovalState = Literal["none", "not_requested", "pending", "approved", "rejected", "invalid"]


class PatientRef(BaseModel):
    patient_id: int
    name: str | None = None


class TaskSummary(BaseModel):
    """List item. Deliberately has no notes, description or approval ids."""

    task_id: int
    patient: PatientRef
    category: str
    title: str
    status: str
    latest_decision: Literal["ALLOW", "BLOCK", "HOLD"] | None = None
    requires_physician_approval: bool
    approval_status: ApprovalState
    created_at: str | None = None
    updated_at: str | None = None


class TaskEvent(BaseModel):
    """One history row. `internal_note` is for the assigned Technician only."""

    event_id: int
    event_type: str
    actor_role: str
    by_me: bool
    decision: Literal["ALLOW", "BLOCK", "HOLD"] | None = None
    hold_reason: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    guardrail_decision: str | None = None
    guardrail_reason: str | None = None
    internal_note: str | None = None
    patient_message: str | None = None
    requester_note: str | None = None
    created_at: str | None = None


class AppointmentInfo(BaseModel):
    """Scheduling facts for an appointment request, read from the appointments table."""

    appointment_id: int
    physician_id: int | None = None
    physician_name: str | None = None
    starts_at: str
    ends_at: str
    timezone: str
    local_label: str
    reason: str | None = None  # the patient's own words
    status: str
    status_label: str
    patient_response: str | None = None  # latest reply to a question, if any


class SlotCheck(BaseModel):
    task_id: int
    result: Literal[
        "ok", "already_confirmed", "slot_unavailable", "physician_unavailable", "not_pending"
    ]
    bookable: bool
    message: str


CorrectionAction = Literal[
    "request_physician_authorization", "request_information", "decline", "close",
    "ready_for_review",  # 014 name, accepted as an alias of request_physician_authorization
]
DeclineReasonCode = Literal[
    "duplicate_request", "incomplete_information", "withdrawn_by_patient", "not_a_correction_request"]


class DeclineReason(BaseModel):
    code: DeclineReasonCode
    label: str


class CorrectionInfo(BaseModel):
    """Coordination facts for a record-correction task. Never contains the correction text or the Physician."""

    request_id: int
    approval_status: ApprovalState
    reviewed_at: str | None = None
    allowed_actions: list[CorrectionAction]
    decline_reasons: list[DeclineReason] = []


class CorrectionActionRequest(BaseModel):
    action: CorrectionAction
    reason_code: DeclineReasonCode | None = None
    internal_note: str | None = Field(default=None, max_length=2000)
    patient_message: str | None = Field(default=None, max_length=500)

    model_config = {"extra": "forbid"}

    @field_validator("internal_note", "patient_message")
    @classmethod
    def _strip_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class CorrectionActionResponse(BaseModel):
    task_id: int
    status: str
    action: CorrectionAction
    outcome: str | None = None
    message: str


class TaskDetail(TaskSummary):
    description: str | None = None
    patient_message: str | None = None
    appointment: AppointmentInfo | None = None
    correction: CorrectionInfo | None = None
    events: list[TaskEvent]


class TaskList(BaseModel):
    items: list[TaskSummary]
    limit: int
    offset: int


class ApprovalSummary(BaseModel):
    task_id: int
    patient: PatientRef
    category: str
    task_status: str
    approval_status: ApprovalState


class TechnicianDashboard(BaseModel):
    assigned_patients: int
    open_tasks: int
    pending_review: int
    in_progress: int
    on_hold: int
    awaiting_physician: int
    completed: int
    recent_tasks: list[TaskSummary]


class DecisionRequest(BaseModel):
    decision: Literal["ALLOW", "BLOCK", "HOLD"]
    internal_note: str | None = Field(default=None, max_length=2000)
    patient_message: str | None = Field(default=None, max_length=500)
    hold_reason: Literal["needs_information", "physician_approval"] | None = None

    model_config = {"extra": "forbid"}

    @field_validator("internal_note", "patient_message")
    @classmethod
    def _strip_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    def validate_for_decision(self) -> None:
        """Raises ValueError with a safe message. Mirrors the database CHECK rules."""
        if self.decision == "HOLD" and self.hold_reason is None:
            raise ValueError("Choose why the request is on hold.")
        if self.decision != "HOLD" and self.hold_reason is not None:
            raise ValueError("A hold reason applies only to HOLD.")
        if self.decision in ("BLOCK", "HOLD") and (not self.internal_note or not self.patient_message):
            raise ValueError("BLOCK and HOLD require an internal reason and a patient-facing message.")


class DecisionResponse(BaseModel):
    outcome: Literal[
        "executed", "recorded", "approval_requested", "approval_reused", "rejected", "escalated"
    ]
    guardrail_decision: Literal["Allow", "Block", "Escalate"]
    guardrail_reason: str
    explanation: str
    task_id: int
    status: str
    latest_decision: Literal["ALLOW", "BLOCK", "HOLD"] | None = None
    approval_status: Literal["none", "pending"] | None = None


class CompleteRequest(BaseModel):
    patient_message: str | None = Field(default=None, max_length=500)
    model_config = {"extra": "forbid"}


class SimpleStatusResponse(BaseModel):
    task_id: int
    status: str
