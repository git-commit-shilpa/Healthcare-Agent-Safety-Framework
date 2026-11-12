import re
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$|^24:00$")


def hhmm_to_minutes(value: str) -> int:
    if not _HHMM.match(value):
        raise ValueError("Use 24-hour HH:MM times, for example 09:00.")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def minutes_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class ScheduleRuleIn(BaseModel):
    """One recurring weekly working window, in the doctor's LOCAL time."""

    rule_id: int | None = Field(default=None, gt=0)  # omit to create, set to edit your own rule
    weekday: int = Field(ge=0, le=6, description="0 = Sunday ... 6 = Saturday")
    start_time: str
    end_time: str
    slot_minutes: int = Field(ge=10, le=120)
    timezone: str = Field(min_length=1, max_length=64)
    effective_from: date | None = None
    effective_to: date | None = None

    model_config = {"extra": "forbid"}

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError) as error:
            raise ValueError("Unknown time zone. Use an IANA name such as America/New_York.") from error
        return value

    @model_validator(mode="after")
    def _window(self) -> "ScheduleRuleIn":
        start, end = hhmm_to_minutes(self.start_time), hhmm_to_minutes(self.end_time)
        if end <= start:
            raise ValueError("The end time must be after the start time.")
        if (end - start) % self.slot_minutes != 0:
            raise ValueError("The working window must divide evenly into appointment slots.")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("The end date cannot be before the start date.")
        return self

    @property
    def start_minute(self) -> int:
        return hhmm_to_minutes(self.start_time)

    @property
    def end_minute(self) -> int:
        return hhmm_to_minutes(self.end_time)


class ScheduleRuleOut(BaseModel):
    rule_id: int
    weekday: int
    start_time: str
    end_time: str
    slot_minutes: int
    timezone: str
    effective_from: str | None = None
    effective_to: str | None = None
    is_active: bool


class ScheduleExceptionIn(BaseModel):
    """Time off / a blocked period ('unavailable'), or a one-off opening ('extra_availability')."""

    kind: Literal["unavailable", "extra_availability"]
    starts_at: datetime
    ends_at: datetime
    slot_minutes: int | None = Field(default=None, ge=10, le=120)
    reason: str | None = Field(default=None, max_length=200)

    model_config = {"extra": "forbid"}

    @field_validator("starts_at", "ends_at", mode="before")
    @classmethod
    def _iso_text_only(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("Use an ISO-8601 date-time string with a time zone.")
        return value

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("The time must include a time zone.")
        return value

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @model_validator(mode="after")
    def _shape(self) -> "ScheduleExceptionIn":
        if self.ends_at <= self.starts_at:
            raise ValueError("The end must be after the start.")
        if (self.ends_at - self.starts_at).days > 31:
            raise ValueError("A single exception can cover at most 31 days.")
        if self.kind == "extra_availability" and self.slot_minutes is None:
            raise ValueError("Extra availability needs an appointment length (slot_minutes).")
        if self.kind == "unavailable" and self.slot_minutes is not None:
            raise ValueError("Unavailable periods do not use slot_minutes.")
        return self


class ScheduleExceptionOut(BaseModel):
    exception_id: int
    kind: str
    starts_at: str
    ends_at: str
    slot_minutes: int | None = None
    reason: str | None = None


class SavedResponse(BaseModel):
    id: int


class PhysicianAppointment(BaseModel):
    """What a Physician sees about THEIR OWN appointments. No clinical data, no free text."""

    appointment_id: int
    request_id: int
    patient_name: str | None = None
    starts_at: str
    ends_at: str
    local_label: str
    status: str
