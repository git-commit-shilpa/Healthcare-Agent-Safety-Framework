from typing import Literal

from pydantic import BaseModel, Field, field_validator

Category = Literal[
    "appointment_scheduling", "test_result_upload", "sample_collection",
    "supplies_equipment", "documentation_request", "general_inquiry",
    "medication_request", "treatment_change_request", "diagnosis_inquiry",
]


class CategoryInfo(BaseModel):
    value: str
    label: str
    requires_physician_review: bool


class ServiceRequestCreate(BaseModel):
    category: Category
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)

    model_config = {"extra": "forbid"}

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A title is required.")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class ServiceRequestInfo(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    model_config = {"extra": "forbid"}

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A message is required.")
        return value


class SubmitResponse(BaseModel):
    task_id: int
    status: str
    requires_physician_review: bool


class StatusResponse(BaseModel):
    task_id: int
    status: str


class ServiceRequestItem(BaseModel):
    """What the Patient may see. No technician identity, notes, decisions or approval ids."""

    task_id: int
    category: str
    category_label: str
    title: str
    description: str | None = None
    status: str
    patient_message: str | None = None
    requires_physician_review: bool
    can_cancel: bool
    can_add_info: bool
    created_at: str | None = None
    updated_at: str | None = None
