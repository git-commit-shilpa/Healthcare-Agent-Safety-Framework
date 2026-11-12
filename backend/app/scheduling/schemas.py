from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PhysicianOption(BaseModel):
    physician_id: int
    name: str
    timezone: str | None = None


class SlotOption(BaseModel):
    start: str  # ISO-8601 UTC instant: send this back unchanged when booking
    end: str
    local_label: str  # e.g. "Mon, 17 Nov 2026, 9:00 AM EST" in the doctor's time zone


class AvailabilityResponse(BaseModel):
    physician_id: int
    timezone: str | None = None
    date: str
    slots: list[SlotOption]


class AppointmentCreate(BaseModel):
    physician_id: int = Field(gt=0)
    starts_at: datetime
    reason: str | None = Field(default=None, max_length=500)
    # The patient must explicitly confirm the summary shown in the UI.
    confirmed: bool

    model_config = {"extra": "forbid"}

    @field_validator("starts_at", mode="before")
    @classmethod
    def _iso_text_only(cls, value: object) -> object:
        # Pydantic would otherwise read a bare number as a Unix timestamp.
        if not isinstance(value, str):
            raise ValueError("The appointment time must be an ISO-8601 date-time string.")
        return value

    @field_validator("starts_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("The appointment time must include a time zone.")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class AppointmentReply(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    model_config = {"extra": "forbid"}

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A message is required.")
        return value


Group = Literal["upcoming", "pending", "completed", "rejected"]


class SubmitAppointmentResponse(BaseModel):
    request_id: int
    appointment_id: int
    status: str
    status_label: str
    starts_at: str
    ends_at: str
    timezone: str
    booked: bool = False  # never true here: a Technician must confirm first
    message: str


class StatusResponse(BaseModel):
    request_id: int
    status: str
    status_label: str


class AppointmentItem(BaseModel):
    """What the Patient may see. No technician identity, internal notes or guardrail data."""

    request_id: int
    appointment_id: int
    physician_name: str | None = None
    starts_at: str
    ends_at: str
    timezone: str
    local_label: str
    reason: str | None = None
    status: str
    status_label: str
    group: Group
    patient_message: str | None = None
    can_cancel: bool
    can_add_info: bool
    created_at: str | None = None


class TimelineEntry(BaseModel):
    at: str | None = None
    kind: str
    title: str
    text: str | None = None


class AppointmentDetail(AppointmentItem):
    timeline: list[TimelineEntry]
