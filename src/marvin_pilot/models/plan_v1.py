"""Closed, versioned schema for Marvin change plans."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
)

from marvin_pilot.duration import normalize_duration

_OPERATION_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_MONTH_RE = re.compile(r"\d{4}-\d{2}\Z")
_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")


class ClosedModel(BaseModel):
    """A plan object that rejects misspelled or invented properties."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _non_empty(value: str, field_name: str, *, maximum: int) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return value


def _iso_date(value: str, field_name: str) -> str:
    if not _DATE_RE.fullmatch(value):
        raise ValueError(f"{field_name} must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a real calendar date") from exc
    return value


def _rfc3339_with_offset(value: str, field_name: str) -> str:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit UTC offset")
    return value


class ParentRef(ClosedModel):
    id: StrictStr
    title: StrictStr | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "parent.id", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "parent.title", maximum=1_000)


class LabelRef(ClosedModel):
    id: StrictStr
    title: StrictStr | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "labels[].id", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "labels[].title", maximum=1_000)


class TaskFields(ClosedModel):
    """Normal, allowlisted task fields; unset and explicit null remain distinguishable."""

    title: StrictStr | None = None
    parent: ParentRef | None = None
    scheduledDate: StrictStr | None = None
    dueDate: StrictStr | None = None
    startDate: StrictStr | None = None
    endDate: StrictStr | None = None
    plannedWeek: StrictStr | None = None
    plannedMonth: StrictStr | None = None
    labels: list[LabelRef] | None = None
    estimatedTimeDuration: StrictStr | None = None
    note: StrictStr | None = None
    dayRank: StrictFloat | StrictInt | None = None
    masterRank: StrictFloat | StrictInt | None = None
    dailySection: Literal["Morning", "Afternoon", "Evening"] | None = None
    bonusSection: Literal["Essential", "Bonus"] | None = None
    customSectionId: StrictStr | None = None
    timeBlockSectionId: StrictStr | None = None
    starPriority: Literal["yellow", "orange", "red"] | None = None
    frogSize: Literal["normal", "baby", "monster"] | None = None
    backburner: StrictBool | None = None
    reviewDate: StrictStr | None = None
    snoozedUntil: StrictStr | None = None
    permanentSnoozeUntil: StrictStr | None = None
    dependencies: list[StrictStr] | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return None if value is None else _non_empty(value, "title", maximum=1_000)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 200_000:
            raise ValueError("note must be at most 200000 characters")
        return value

    @field_validator("scheduledDate", "dueDate", "startDate", "endDate", "reviewDate")
    @classmethod
    def validate_dates(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _iso_date(value, getattr(info, "field_name", "date"))

    @field_validator("plannedWeek")
    @classmethod
    def validate_planned_week(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _iso_date(value, "plannedWeek")
        if date.fromisoformat(value).weekday() != 0:
            raise ValueError("plannedWeek must be a Monday")
        return value

    @field_validator("plannedMonth")
    @classmethod
    def validate_planned_month(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _MONTH_RE.fullmatch(value):
            raise ValueError("plannedMonth must use YYYY-MM")
        try:
            date.fromisoformat(f"{value}-01")
        except ValueError as exc:
            raise ValueError("plannedMonth is not a real calendar month") from exc
        return value

    @field_validator("estimatedTimeDuration")
    @classmethod
    def validate_estimate(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                return normalize_duration(value)
            except Exception as exc:
                raise ValueError(str(exc)) from exc
        return value

    @field_validator("snoozedUntil")
    @classmethod
    def validate_snooze(cls, value: str | None) -> str | None:
        return None if value is None else _rfc3339_with_offset(value, "snoozedUntil")

    @field_validator("permanentSnoozeUntil")
    @classmethod
    def validate_permanent_snooze(cls, value: str | None) -> str | None:
        if value is not None and not _TIME_RE.fullmatch(value):
            raise ValueError("permanentSnoozeUntil must use HH:mm")
        return value

    @field_validator("dayRank", "masterRank")
    @classmethod
    def validate_finite_rank(cls, value: float | int | None) -> float | int | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("rank values must be finite")
        return value

    @field_validator("customSectionId", "timeBlockSectionId")
    @classmethod
    def validate_optional_id(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _non_empty(value, getattr(info, "field_name", "id"), maximum=500)

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: list[LabelRef] | None) -> list[LabelRef] | None:
        if value is not None:
            ids = [item.id for item in value]
            if len(ids) != len(set(ids)):
                raise ValueError("labels must contain unique IDs")
        return value

    @field_validator("dependencies")
    @classmethod
    def validate_dependencies(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for dependency_id in value:
            _non_empty(dependency_id, "dependencies[]", maximum=500)
        if len(value) != len(set(value)):
            raise ValueError("dependencies must contain unique IDs")
        return value


class CreateTaskFields(TaskFields):
    title: StrictStr

    @field_validator("title")
    @classmethod
    def validate_required_title(cls, value: str) -> str:
        return _non_empty(value, "title", maximum=1_000)


class ExistingTaskTarget(ClosedModel):
    type: Literal["task"]
    id: StrictStr
    title: StrictStr

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "target.id", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _non_empty(value, "target.title", maximum=1_000)


class NewTaskTarget(ClosedModel):
    type: Literal["task"]
    id: StrictStr

    @field_validator("id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("target.id for create must be a UUID") from exc
        return str(parsed)


class BaseOperation(ClosedModel):
    operationId: StrictStr
    reason: StrictStr
    dependsOnOperations: list[StrictStr] = Field(default_factory=list)

    @field_validator("operationId")
    @classmethod
    def validate_operation_id(cls, value: str) -> str:
        if not _OPERATION_ID_RE.fullmatch(value):
            raise ValueError(
                "operationId must contain lowercase letters, digits, and internal hyphens only"
            )
        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        return _non_empty(value, "reason", maximum=2_000)

    @field_validator("dependsOnOperations")
    @classmethod
    def validate_operation_dependencies(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("dependsOnOperations must contain unique operation IDs")
        for operation_id in value:
            if not _OPERATION_ID_RE.fullmatch(operation_id):
                raise ValueError(f"invalid dependsOnOperations ID: {operation_id!r}")
        return value


class UpdateOperation(BaseOperation):
    action: Literal["update"]
    target: ExistingTaskTarget
    before: TaskFields
    after: TaskFields
    expectedUpdatedAt: Annotated[StrictInt, Field(ge=0)] | None = None


class CreateOperation(BaseOperation):
    action: Literal["create"]
    target: NewTaskTarget
    after: CreateTaskFields


class TrashOperation(BaseOperation):
    action: Literal["trash"]
    target: ExistingTaskTarget
    expectedUpdatedAt: Annotated[StrictInt, Field(ge=0)] | None = None


Operation = Annotated[
    UpdateOperation | CreateOperation | TrashOperation,
    Field(discriminator="action"),
]


class ChangePlanV1(ClosedModel):
    schema_: StrictStr | None = Field(default=None, alias="$schema")
    schemaVersion: Literal[1]
    planId: StrictStr
    createdAt: StrictStr
    summary: StrictStr
    operations: Annotated[list[Operation], Field(min_length=1, max_length=500)]

    @field_validator("schemaVersion", mode="before")
    @classmethod
    def validate_schema_version_type(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("schemaVersion must be the integer 1")
        return value

    @field_validator("schema_")
    @classmethod
    def validate_schema_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("$schema must be an HTTPS URL")
        return value

    @field_validator("planId")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("planId must be a UUID") from exc
        return str(parsed)

    @field_validator("createdAt")
    @classmethod
    def validate_created_at(cls, value: str) -> str:
        return _rfc3339_with_offset(value, "createdAt")

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _non_empty(value, "summary", maximum=2_000)
