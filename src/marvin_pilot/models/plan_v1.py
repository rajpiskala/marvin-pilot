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
    model_validator,
)

from marvin_pilot.completion import completion_day_behavior, completion_local_date
from marvin_pilot.duration import normalize_duration

_OPERATION_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_MONTH_RE = re.compile(r"\d{4}-\d{2}\Z")
_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_DISPLAY_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")


class ClosedModel(BaseModel):
    """A plan object that rejects misspelled or invented properties."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ExpectedAccount(ClosedModel):
    """Stable account identity that every live command must verify first."""

    userId: StrictStr
    email: StrictStr

    @field_validator("userId")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("expectedAccount.userId must be a numeric Marvin user ID")
        return _non_empty(value, "expectedAccount.userId", maximum=100)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = _non_empty(value, "expectedAccount.email", maximum=320)
        if "@" not in value or any(ord(character) < 32 for character in value):
            raise ValueError("expectedAccount.email must be a valid email address")
        return value


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


SubtaskAcceptedLoss = Literal[
    "scheduledDate",
    "dueDate",
    "startDate",
    "endDate",
    "plannedWeek",
    "plannedMonth",
    "labels",
    "estimatedTimeDuration",
    "note",
    "dailySection",
    "bonusSection",
    "customSectionId",
    "timeBlockSectionId",
    "starPriority",
    "frogSize",
    "backburner",
    "reviewDate",
    "snoozedUntil",
    "permanentSnoozeUntil",
]


class SubtaskSourceRef(ClosedModel):
    """Optional provenance for safely converting a loose task into a subtask."""

    id: StrictStr
    title: StrictStr
    acceptLoss: list[SubtaskAcceptedLoss] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "subtasks[].sourceTask.id", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _non_empty(value, "subtasks[].sourceTask.title", maximum=1_000)

    @field_validator("acceptLoss")
    @classmethod
    def validate_accept_loss(cls, value: list[SubtaskAcceptedLoss]) -> list[SubtaskAcceptedLoss]:
        if len(value) != len(set(value)):
            raise ValueError("subtasks[].sourceTask.acceptLoss must contain unique fields")
        return value


class SubtaskFields(ClosedModel):
    """Ordered, reviewable subset of one Marvin embedded subtask."""

    id: StrictStr
    title: StrictStr
    done: StrictBool = False
    sourceTask: SubtaskSourceRef | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "subtasks[].id", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _non_empty(value, "subtasks[].title", maximum=1_000)


class RecurringSubtaskFields(ClosedModel):
    """One ordered, initially-open subtask copied into every generated occurrence."""

    id: StrictStr
    title: StrictStr

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "subtasks[].id", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _non_empty(value, "subtasks[].title", maximum=1_000)


class RecurrenceCadenceBase(ClosedModel):
    """Shared, explicit anchor and optional end date for a recurrence series."""

    startDate: StrictStr
    endDate: StrictStr | None = None

    @field_validator("startDate")
    @classmethod
    def validate_start_date(cls, value: str) -> str:
        return _iso_date(value, "cadence.startDate")

    @field_validator("endDate")
    @classmethod
    def validate_end_date(cls, value: str | None) -> str | None:
        return None if value is None else _iso_date(value, "cadence.endDate")

    @model_validator(mode="after")
    def validate_date_order(self) -> RecurrenceCadenceBase:
        if self.endDate is not None and self.endDate < self.startDate:
            raise ValueError("cadence.endDate must not be earlier than cadence.startDate")
        return self


class DailyCadence(RecurrenceCadenceBase):
    type: Literal["daily"]


class WeeklyCadence(RecurrenceCadenceBase):
    type: Literal["weekly"]
    weekday: Annotated[StrictInt, Field(ge=0, le=6)]


class MonthlyCadence(RecurrenceCadenceBase):
    type: Literal["monthly"]
    monthDate: Annotated[StrictInt, Field(ge=1, le=31)]
    limitToWeekdays: StrictBool = False


class NPerWeekCadence(RecurrenceCadenceBase):
    type: Literal["n per week"]
    weekdays: Annotated[list[Annotated[StrictInt, Field(ge=0, le=6)]], Field(min_length=1)]

    @field_validator("weekdays")
    @classmethod
    def validate_weekdays(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("cadence.weekdays must contain unique weekday numbers")
        return value


class RepeatCadence(RecurrenceCadenceBase):
    type: Literal["repeat", "repeat week", "repeat month", "repeat year"]
    interval: Annotated[StrictInt, Field(ge=1, le=10_000)]
    limitToWeekdays: StrictBool = False

    @model_validator(mode="after")
    def validate_weekday_limit(self) -> RepeatCadence:
        if self.limitToWeekdays and self.type != "repeat month":
            raise ValueError("cadence.limitToWeekdays is only valid for repeat month")
        return self


class EchoCadence(RecurrenceCadenceBase):
    type: Literal["echo"]
    daysAfterCompletion: Annotated[StrictInt, Field(ge=1, le=100_000)]


class OnOffCadence(RecurrenceCadenceBase):
    type: Literal["onOff"]
    onDays: Annotated[StrictInt, Field(ge=1, le=100_000)]
    offDays: Annotated[StrictInt, Field(ge=1, le=100_000)]


class CustomCadence(RecurrenceCadenceBase):
    type: Literal["custom"]
    expression: StrictStr

    @field_validator("expression")
    @classmethod
    def validate_expression(cls, value: str) -> str:
        return _non_empty(value, "cadence.expression", maximum=2_000)


RecurrenceCadence = Annotated[
    DailyCadence
    | WeeklyCadence
    | MonthlyCadence
    | NPerWeekCadence
    | RepeatCadence
    | EchoCadence
    | OnOffCadence
    | CustomCadence,
    Field(discriminator="type"),
]


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
    subtasks: Annotated[list[SubtaskFields], Field(max_length=500)] | None = None
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
    orbit: StrictBool | None = None

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

    @field_validator("subtasks")
    @classmethod
    def validate_subtasks(cls, value: list[SubtaskFields] | None) -> list[SubtaskFields] | None:
        if value is None:
            return None
        ids = [item.id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("subtasks must contain unique IDs")
        source_ids = [item.sourceTask.id for item in value if item.sourceTask is not None]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("subtasks must not convert the same source task more than once")
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


class RecurringTaskFields(ClosedModel):
    """Allowlisted fields stored on a recurring-task series template."""

    title: StrictStr | None = None
    parent: ParentRef | None = None
    labels: list[LabelRef] | None = None
    estimatedTimeDuration: StrictStr | None = None
    note: StrictStr | None = None
    subtasks: Annotated[list[RecurringSubtaskFields], Field(max_length=500)] | None = None
    starPriority: Literal["yellow", "orange", "red"] | None = None
    frogSize: Literal["normal", "baby", "monster"] | None = None
    dueInDays: Annotated[StrictInt, Field(ge=0, le=100_000)] | None = None
    cadence: RecurrenceCadence | None = None

    @field_validator("cadence", mode="before")
    @classmethod
    def validate_cadence_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("cadence cannot be null; trash the series to stop it")
        return value

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

    @field_validator("estimatedTimeDuration")
    @classmethod
    def validate_estimate(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                return normalize_duration(value)
            except Exception as exc:
                raise ValueError(str(exc)) from exc
        return value

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: list[LabelRef] | None) -> list[LabelRef] | None:
        if value is not None:
            ids = [item.id for item in value]
            if len(ids) != len(set(ids)):
                raise ValueError("labels must contain unique IDs")
        return value

    @field_validator("subtasks")
    @classmethod
    def validate_subtasks(
        cls, value: list[RecurringSubtaskFields] | None
    ) -> list[RecurringSubtaskFields] | None:
        if value is not None:
            ids = [item.id for item in value]
            if len(ids) != len(set(ids)):
                raise ValueError("subtasks must contain unique IDs")
        return value


class CreateRecurringTaskFields(RecurringTaskFields):
    title: StrictStr
    cadence: RecurrenceCadence

    @field_validator("title")
    @classmethod
    def validate_required_title(cls, value: str) -> str:
        return _non_empty(value, "title", maximum=1_000)


ItemFields = TaskFields | RecurringTaskFields
CreateItemFields = CreateTaskFields | CreateRecurringTaskFields


class RecurringOccurrenceRef(ClosedModel):
    """Exact series/date identity required to mutate one generated occurrence."""

    scope: Literal["occurrence"] = "occurrence"
    seriesId: StrictStr
    seriesTitle: StrictStr
    scheduledDate: StrictStr

    @field_validator("seriesId")
    @classmethod
    def validate_series_id(cls, value: str) -> str:
        return _non_empty(value, "target.recurrence.seriesId", maximum=500)

    @field_validator("seriesTitle")
    @classmethod
    def validate_series_title(cls, value: str) -> str:
        return _non_empty(value, "target.recurrence.seriesTitle", maximum=1_000)

    @field_validator("scheduledDate")
    @classmethod
    def validate_scheduled_date(cls, value: str) -> str:
        return _iso_date(value, "target.recurrence.scheduledDate")


class ExistingItemTarget(ClosedModel):
    type: Literal["task", "project", "category", "recurringTask"]
    id: StrictStr
    title: StrictStr
    recurrence: RecurringOccurrenceRef | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "target.id", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _non_empty(value, "target.title", maximum=1_000)

    @model_validator(mode="after")
    def validate_recurrence_target(self) -> ExistingItemTarget:
        if self.recurrence is not None and self.type != "task":
            raise ValueError("target.recurrence is only valid for a task occurrence")
        return self


class NewItemTarget(ClosedModel):
    type: Literal["task", "project", "category", "recurringTask"]
    id: StrictStr

    @field_validator("id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("target.id for create must be a UUID") from exc
        return str(parsed)


# Retain the original import names for callers that constructed task-only v1 models directly.
ExistingTaskTarget = ExistingItemTarget
NewTaskTarget = NewItemTarget


class HierarchyPathNode(ClosedModel):
    """Review-only Marvin ancestry for one side of an operation."""

    id: StrictStr
    type: Literal["inbox", "category", "project", "task"]
    title: StrictStr
    emoji: StrictStr | None = None
    color: StrictStr | None = None
    order: Annotated[StrictInt, Field(ge=-1_000_000, le=1_000_000)] | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _non_empty(value, "display path node id", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _non_empty(value, "display path node title", maximum=1_000)

    @field_validator("emoji")
    @classmethod
    def validate_emoji(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_empty(value, "display path node emoji", maximum=32)

    @field_validator("color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is not None and not _DISPLAY_COLOR_RE.fullmatch(value):
            raise ValueError("display path node color must use #RRGGBB")
        return value.lower() if value is not None else None


class DaySectionRef(ClosedModel):
    """Visible Today-list grouping supplied for offline review."""

    key: StrictStr
    title: StrictStr
    order: Annotated[StrictInt, Field(ge=-1_000_000, le=1_000_000)]

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        return _non_empty(value, "day section key", maximum=500)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _non_empty(value, "day section title", maximum=200)


class ReviewDisplay(ClosedModel):
    """Plan-level visualizer recommendation with no execution effect."""

    showDaySectionsByDefault: StrictBool = False


class OperationDisplay(ClosedModel):
    """Optional review-only metadata; it never authorizes a Marvin mutation."""

    beforeSection: StrictStr | None = None
    afterSection: StrictStr | None = None
    beforePath: list[HierarchyPathNode] | None = None
    afterPath: list[HierarchyPathNode] | None = None
    beforeOrder: Annotated[StrictInt, Field(ge=-1_000_000, le=1_000_000)] | None = None
    afterOrder: Annotated[StrictInt, Field(ge=-1_000_000, le=1_000_000)] | None = None
    beforeDaySection: DaySectionRef | None = None
    afterDaySection: DaySectionRef | None = None
    existingCompletedAt: StrictStr | None = None

    @field_validator("beforeSection", "afterSection")
    @classmethod
    def validate_section_title(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = f"display.{getattr(info, 'field_name', 'section')}"
        return _non_empty(value, field_name, maximum=200)

    @field_validator("beforePath", "afterPath")
    @classmethod
    def validate_path(
        cls, value: list[HierarchyPathNode] | None, info: object
    ) -> list[HierarchyPathNode] | None:
        if value is None:
            return None
        ids = [node.id for node in value]
        if len(ids) != len(set(ids)):
            field_name = getattr(info, "field_name", "path")
            raise ValueError(f"display.{field_name} must contain unique IDs")
        return value

    @field_validator("existingCompletedAt")
    @classmethod
    def validate_existing_completed_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _rfc3339_with_offset(value, "display.existingCompletedAt")


class SiblingOrder(ClosedModel):
    """Relative list placement resolved to a native rank during live preflight."""

    beforeId: StrictStr | None = None
    afterId: StrictStr | None = None
    position: Literal["first", "last"] | None = None

    @field_validator("beforeId", "afterId")
    @classmethod
    def validate_anchor_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_empty(value, "siblingOrder anchor ID", maximum=500)

    @model_validator(mode="after")
    def validate_one_selector(self) -> SiblingOrder:
        selected = sum(value is not None for value in (self.beforeId, self.afterId, self.position))
        if selected != 1:
            raise ValueError("siblingOrder requires exactly one of beforeId, afterId, or position")
        return self


class BaseOperation(ClosedModel):
    operationId: StrictStr
    reason: StrictStr
    dependsOnOperations: list[StrictStr] = Field(default_factory=list)
    display: OperationDisplay | None = None

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

    @model_validator(mode="after")
    def validate_target_not_in_display_path(self) -> BaseOperation:
        target = getattr(self, "target", None)
        if target is None or self.display is None:
            return self
        for field_name in ("beforePath", "afterPath"):
            path = getattr(self.display, field_name)
            if path is not None and any(node.id == target.id for node in path):
                raise ValueError(f"display.{field_name} must contain ancestors only")
        return self


class UpdateOperation(BaseOperation):
    action: Literal["update"]
    target: ExistingItemTarget
    before: ItemFields
    after: ItemFields
    expectedUpdatedAt: Annotated[StrictInt, Field(ge=0)] | None = None
    siblingOrder: SiblingOrder | None = None


class CreateOperation(BaseOperation):
    action: Literal["create"]
    target: NewItemTarget
    after: CreateItemFields


class TrashOperation(BaseOperation):
    action: Literal["trash"]
    target: ExistingItemTarget
    expectedUpdatedAt: Annotated[StrictInt, Field(ge=0)] | None = None


class CompletionDayTransition(ClosedModel):
    """Review lock for the Marvin history bucket used by a task completion."""

    before: StrictStr | None
    after: StrictStr
    behavior: Literal["assigned", "preserved", "replaced"]

    @field_validator("before", "after")
    @classmethod
    def validate_day(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _iso_date(value, f"completionDay.{getattr(info, 'field_name', 'day')}")


class CompleteOperation(BaseOperation):
    action: Literal["complete"]
    target: ExistingItemTarget
    completedAt: StrictStr
    completionDay: CompletionDayTransition | None = None
    expectedUpdatedAt: Annotated[StrictInt, Field(ge=0)] | None = None

    @field_validator("completedAt")
    @classmethod
    def validate_completed_at(cls, value: str) -> str:
        return _rfc3339_with_offset(value, "completedAt")

    @model_validator(mode="after")
    def reject_category_completion(self) -> CompleteOperation:
        if self.target.type == "category":
            raise ValueError("categories cannot be completed; only tasks and projects can")
        if self.target.type == "project" and self.completionDay is not None:
            raise ValueError("completionDay is only valid for task completions")
        if self.target.type == "task" and self.completionDay is not None:
            expected_after = completion_local_date(self.completedAt)
            if self.completionDay.after != expected_after:
                raise ValueError(
                    "completionDay.after must equal the local calendar date encoded by completedAt"
                )
            expected_behavior = completion_day_behavior(
                self.completionDay.before, self.completionDay.after
            )
            if self.completionDay.behavior != expected_behavior:
                raise ValueError("completionDay.behavior does not match completionDay.before/after")
        return self


Operation = Annotated[
    UpdateOperation | CreateOperation | TrashOperation | CompleteOperation,
    Field(discriminator="action"),
]


class ChangePlanV1(ClosedModel):
    schema_: StrictStr | None = Field(default=None, alias="$schema")
    schemaVersion: Literal[1]
    planId: StrictStr
    createdAt: StrictStr
    summary: StrictStr
    expectedAccount: ExpectedAccount | None = None
    reviewDisplay: ReviewDisplay | None = None
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
