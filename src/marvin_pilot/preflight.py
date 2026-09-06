"""Whole-plan live validation before the first Marvin mutation."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from marvin_pilot.compiler import (
    CompiledMutation,
    compile_operation,
    project_compiled_mutation,
)
from marvin_pilot.completion import completion_local_date, usable_marvin_day
from marvin_pilot.errors import LivePreconditionError
from marvin_pilot.field_registry import (
    FIELD_SPECS,
    RECURRING_TASK_FIELD_SPECS,
    live_field_matches,
    recurring_task_field_matches,
)
from marvin_pilot.models.plan_v1 import (
    ChangePlanV1,
    CompleteOperation,
    CreateOperation,
    Operation,
    TrashOperation,
    UpdateOperation,
)


class DocumentReader(Protocol):
    def get_doc(self, item_id: str) -> dict[str, Any] | None: ...

    def get_labels(self) -> list[dict[str, Any]]: ...

    def check_connection(self) -> Any: ...

    def get_children(self, parent_id: str) -> list[dict[str, Any]]: ...


def verify_expected_account(plan: ChangePlanV1, reader: DocumentReader) -> Any | None:
    """Verify the plan's stable account binding before any document reads."""

    expected = plan.expectedAccount
    if expected is None:
        return None
    account = reader.check_connection()
    if account.account_user_id != expected.userId:
        raise LivePreconditionError(
            "plan account mismatch: expected Marvin user ID "
            f"{expected.userId!r} ({expected.email}), connected user ID "
            f"{account.account_user_id!r} ({account.account_email})",
            check="expected-account",
            expected=expected.userId,
            found=account.account_user_id,
        )
    if account.account_email.casefold() != expected.email.casefold():
        raise LivePreconditionError(
            "plan account email mismatch for the expected user ID: expected "
            f"{expected.email!r}, connected {account.account_email!r}",
            check="expected-account-email",
            expected=expected.email,
            found=account.account_email,
        )
    return account


@dataclass(frozen=True, slots=True)
class PreflightOperation:
    operation: Operation
    live_document: dict[str, Any] | None
    compiled: CompiledMutation
    live_revision: dict[str, Any]
    prior_same_target_operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class PreflightDiagnostic:
    severity: Literal["error", "warning"]
    operation_index: int
    operation_id: str
    target_id: str
    check: str
    message: str
    expected: Any = None
    found: Any = None


@dataclass(frozen=True, slots=True)
class PreflightResult:
    plan: ChangePlanV1
    operations: tuple[PreflightOperation, ...]
    checked_at_ms: int
    strict_concurrency: bool
    warnings: tuple[PreflightDiagnostic, ...] = ()
    unique_documents_checked: int = 0
    metadata_collections_checked: int = 0
    elapsed_ms: int = 0


@dataclass(frozen=True, slots=True)
class LiveValidationResult:
    plan: ChangePlanV1
    operations: tuple[PreflightOperation, ...]
    diagnostics: tuple[PreflightDiagnostic, ...]
    selected_indices: tuple[int, ...]
    checked_at_ms: int
    strict_concurrency: bool
    unique_documents_checked: int = 0
    metadata_collections_checked: int = 0
    elapsed_ms: int = 0

    @property
    def errors(self) -> tuple[PreflightDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "error")

    @property
    def warnings(self) -> tuple[PreflightDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity == "warning")

    @property
    def valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class _OperationWarning:
    check: str
    message: str
    expected: Any
    found: Any


def _meaningful(value: Any) -> bool:
    return value is not None and value is not False and value != "" and value != [] and value != {}


def coupled_task_reasons(
    document: dict[str, Any], *, allow_explicit_recurrence: bool = False
) -> list[str]:
    """Return coupled behaviors that make a simple document mutation unsafe in v1."""

    reasons: list[str] = []
    if _meaningful(document.get("echo")):
        reasons.append("recurrence/echo")
    elif _meaningful(document.get("recurring")) and not allow_explicit_recurrence:
        reasons.append("recurrence")
    if _meaningful(document.get("isPinned")) or _meaningful(document.get("pinId")):
        reasons.append("pinned-task copying")
    if _meaningful(document.get("isReward")) or _meaningful(document.get("rewardId")):
        reasons.append("reward side effects")
    if any(_meaningful(document.get(field)) for field in ("isTracking", "tracking")):
        reasons.append("active time tracking")
    reminder_fields = (
        "reminderOffset",
        "reminderTime",
        "snooze",
        "autoSnooze",
        "remindAt",
        "reminder",
    )
    if any(_meaningful(document.get(field)) for field in reminder_fields):
        reasons.append(
            "reminder server state (Pilot cannot yet preserve the separate reminder record; "
            "remove/recreate the reminder in Marvin or make this change by hand)"
        )
    if any(_meaningful(document.get(field)) for field in ("calId", "calURL", "etag", "calData")):
        reasons.append("calendar synchronization")
    return reasons


def revision_snapshot(document: dict[str, Any]) -> dict[str, Any]:
    return {
        field: {"present": field in document, "value": document.get(field)}
        for field in ("_rev", "updatedAt")
    }


def _rfc3339_milliseconds(value: str) -> int:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    return int(datetime.fromisoformat(candidate).timestamp() * 1_000)


def _check_existing_preconditions(
    operation: UpdateOperation | TrashOperation | CompleteOperation,
    document: dict[str, Any],
) -> None:
    expected_db = {
        "task": "Tasks",
        "project": "Categories",
        "category": "Categories",
        "recurringTask": "RecurringTasks",
    }[operation.target.type]
    if (
        document.get("db") != expected_db
        or (
            operation.target.type in {"project", "category"}
            and document.get("type") != operation.target.type
        )
        or (operation.target.type == "recurringTask" and document.get("recurringType") != "task")
    ):
        document_kind = {
            "task": "a non-Task",
            "project": "not a live project",
            "category": "not a live category",
            "recurringTask": "not a live recurring-task series",
        }[operation.target.type]
        raise LivePreconditionError(
            f"operation {operation.operationId!r} target is {document_kind} document"
        )
    if document.get("title") != operation.target.title:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} title is stale: expected "
            f"{operation.target.title!r}, found {document.get('title')!r}",
            check="target-title",
            expected=operation.target.title,
            found=document.get("title"),
        )
    if (
        operation.expectedUpdatedAt is not None
        and document.get("updatedAt") != operation.expectedUpdatedAt
    ):
        raise LivePreconditionError(
            f"operation {operation.operationId!r} updatedAt is stale: expected "
            f"{operation.expectedUpdatedAt}, found {document.get('updatedAt')!r}",
            check="updated-at",
            expected=operation.expectedUpdatedAt,
            found=document.get("updatedAt"),
        )
    explicit_occurrence = (
        operation.target.type == "task" and operation.target.recurrence is not None
    )
    reasons = coupled_task_reasons(
        document,
        allow_explicit_recurrence=explicit_occurrence or operation.target.type == "recurringTask",
    )
    if reasons:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} is blocked by coupled behavior: "
            + ", ".join(reasons)
        )
    if _meaningful(document.get("deletedAt")):
        if isinstance(operation, TrashOperation):
            raise LivePreconditionError(
                f"operation {operation.operationId!r} targets an item already in Trash"
            )
        raise LivePreconditionError(
            f"operation {operation.operationId!r} targets an item currently in Trash"
        )
    if isinstance(operation, CompleteOperation):
        already_done = document.get("done") is True
        if operation.repairHistory:
            if not already_done:
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} cannot repair completion history "
                    "because the project is open"
                )
            live_done_at = document.get("doneAt")
            if _meaningful(live_done_at):
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} refuses to overwrite existing "
                    f"project doneAt {live_done_at!r}"
                )
            expected_day = completion_local_date(operation.completedAt)
            if document.get("doneDate") != expected_day:
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} project doneDate is stale: expected "
                    f"{expected_day!r}, found {document.get('doneDate')!r}",
                    check="completion-done-date",
                    expected=expected_day,
                    found=document.get("doneDate"),
                )
        elif already_done:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} targets an item already completed"
            )
    if (
        isinstance(operation, CompleteOperation)
        and operation.target.type in {"task", "project"}
        and operation.completionDay is not None
    ):
        live_day = usable_marvin_day(document.get("day"))
        if live_day != operation.completionDay.before:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} completion day is stale: expected "
                f"{operation.completionDay.before!r}, found {live_day!r}",
                check="completion-day",
                expected=operation.completionDay.before,
                found=live_day,
            )
    existing_completed_at = (
        operation.display.existingCompletedAt if operation.display is not None else None
    )
    if (
        isinstance(operation, UpdateOperation)
        and operation.target.type == "task"
        and document.get("done") is True
        and existing_completed_at is None
    ):
        raise LivePreconditionError(
            f"operation {operation.operationId!r} targets a completed task; add its exact "
            "display.existingCompletedAt so review shows that the task stays completed"
        )
    if existing_completed_at is not None:
        if document.get("done") is not True:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} declares an existing completion, but the "
                "live task is open"
            )
        live_done_at = document.get("doneAt")
        if isinstance(live_done_at, bool) or not isinstance(live_done_at, (int, float)):
            raise LivePreconditionError(
                f"operation {operation.operationId!r} declares an existing completion, but the "
                "live task has no exact doneAt timestamp"
            )
        expected_done_at = _rfc3339_milliseconds(existing_completed_at)
        if int(live_done_at) != expected_done_at:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} completion timestamp is stale: expected "
                f"{expected_done_at}, found {live_done_at!r}"
            )
    if isinstance(operation, UpdateOperation):
        mismatches = []
        before = operation.before.model_dump(exclude_unset=True, mode="json")
        for field, expected in before.items():
            if operation.target.type == "recurringTask":
                matches = recurring_task_field_matches(field, expected, document)
                spec = RECURRING_TASK_FIELD_SPECS[field]
                marvin_field = spec.marvin_name or "recurrence cadence"
            else:
                matches = live_field_matches(field, expected, document)
                marvin_field = FIELD_SPECS[field].marvin_name
            if not matches:
                mismatches.append(
                    f"{field} expected {expected!r}, live {document.get(marvin_field)!r}"
                )
        if mismatches:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} has stale fields: " + "; ".join(mismatches)
            )


def _verify_recurrence_identity(
    operation: Operation,
    document: dict[str, Any] | None,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
) -> None:
    """Require explicit series/date identity for every generated occurrence mutation."""

    if isinstance(operation, CreateOperation) or operation.target.type != "task":
        return
    recurrence = operation.target.recurrence
    is_recurring = document is not None and (
        document.get("recurring") is True or _meaningful(document.get("recurringTaskId"))
    )
    if is_recurring and recurrence is None:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} targets a generated recurring occurrence; "
            "add target.recurrence with the exact series and scheduled date"
        )
    if not is_recurring and recurrence is not None:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} declares an occurrence target, but the live "
            "task is not recurring"
        )
    if recurrence is None or document is None:
        return
    if document.get("recurring") is not True:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} occurrence is missing recurring=true"
        )
    if document.get("recurringTaskId") != recurrence.seriesId:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} recurring series ID is stale: expected "
            f"{recurrence.seriesId!r}, found {document.get('recurringTaskId')!r}",
            check="recurrence-series-id",
            expected=recurrence.seriesId,
            found=document.get("recurringTaskId"),
        )
    if document.get("day") != recurrence.scheduledDate:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} occurrence date is stale: expected "
            f"{recurrence.scheduledDate!r}, found {document.get('day')!r}",
            check="recurrence-date",
            expected=recurrence.scheduledDate,
            found=document.get("day"),
        )
    if recurrence.seriesId not in cache:
        cache[recurrence.seriesId] = reader.get_doc(recurrence.seriesId)
    template = cache[recurrence.seriesId]
    if (
        template is None
        or template.get("db") != "RecurringTasks"
        or template.get("recurringType") != "task"
    ):
        raise LivePreconditionError(
            f"operation {operation.operationId!r} references a missing or non-task recurrence "
            f"series {recurrence.seriesId!r}"
        )
    if template.get("title") != recurrence.seriesTitle:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} recurrence series title is stale: expected "
            f"{recurrence.seriesTitle!r}, found {template.get('title')!r}",
            check="recurrence-series-title",
            expected=recurrence.seriesTitle,
            found=template.get("title"),
        )


def _reference_hints(
    operation: Operation,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    parents: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    dependencies: list[str] = []
    field_sets = []
    if isinstance(operation, UpdateOperation):
        field_sets.extend((operation.before, operation.after))
    elif isinstance(operation, CreateOperation):
        field_sets.append(operation.after)
    for fields in field_sets:
        dumped = fields.model_dump(exclude_unset=True, mode="json")
        if dumped.get("parent") is not None:
            parents.append(dumped["parent"])
        if dumped.get("labels") is not None:
            labels.extend(dumped["labels"])
        if dumped.get("dependencies") is not None:
            dependencies.extend(dumped["dependencies"])
    return parents, labels, dependencies


def _verify_reference(
    *,
    kind: str,
    reference_id: str,
    title_hint: str | None,
    operation_id: str,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
    planned_creates: dict[str, CreateOperation],
) -> _OperationWarning | None:
    if kind == "parent" and reference_id == "unassigned":
        if title_hint is not None and title_hint != "Inbox":
            return _OperationWarning(
                check="parent-title-hint",
                message=(
                    f"operation {operation_id!r} parent title hint is stale for 'unassigned': "
                    f"expected {title_hint!r}, found 'Inbox'"
                ),
                expected=title_hint,
                found="Inbox",
            )
        return None
    planned = planned_creates.get(reference_id)
    if planned is not None:
        if kind == "parent" and planned.target.type not in {"project", "category"}:
            raise LivePreconditionError(
                f"operation {operation_id!r} references newly created non-project parent "
                f"{reference_id!r}"
            )
        planned_title = planned.after.title
        if title_hint is not None and planned_title != title_hint:
            return _OperationWarning(
                check=f"{kind}-title-hint",
                message=(
                    f"operation {operation_id!r} {kind} title hint is stale for planned create "
                    f"{reference_id!r}: expected {title_hint!r}, found {planned_title!r}"
                ),
                expected=title_hint,
                found=planned_title,
            )
        return None
    if reference_id not in cache:
        cache[reference_id] = reader.get_doc(reference_id)
    document = cache[reference_id]
    if document is None:
        raise LivePreconditionError(
            f"operation {operation_id!r} references missing {kind} ID {reference_id!r}"
        )
    if kind == "parent" and (
        document.get("db") != "Categories"
        or document.get("type") not in {None, "category", "project"}
    ):
        raise LivePreconditionError(
            f"operation {operation_id!r} references non-container parent ID {reference_id!r}"
        )
    if title_hint is not None and document.get("title") != title_hint:
        return _OperationWarning(
            check=f"{kind}-title-hint",
            message=(
                f"operation {operation_id!r} {kind} title hint is stale for {reference_id!r}: "
                f"expected {title_hint!r}, found {document.get('title')!r}"
            ),
            expected=title_hint,
            found=document.get("title"),
        )
    return None


def _verify_references(
    operation: Operation,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
    metadata_cache: dict[str, Any],
    planned_creates: dict[str, CreateOperation],
) -> tuple[_OperationWarning, ...]:
    parents, labels, dependencies = _reference_hints(operation)
    seen: set[tuple[str, str, str | None]] = set()
    warnings: list[_OperationWarning] = []
    for parent in parents:
        key = ("parent", parent["id"], parent.get("title"))
        if key not in seen:
            warning = _verify_reference(
                kind="parent",
                reference_id=parent["id"],
                title_hint=parent.get("title"),
                operation_id=operation.operationId,
                reader=reader,
                cache=cache,
                planned_creates=planned_creates,
            )
            if warning is not None:
                warnings.append(warning)
            seen.add(key)
    for label in labels:
        key = ("label", label["id"], label.get("title"))
        if key not in seen:
            if "labels" not in metadata_cache:
                metadata_cache["labels"] = {item["_id"]: item for item in reader.get_labels()}
            label_document = metadata_cache["labels"].get(label["id"])
            if label_document is None:
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} references missing label ID "
                    f"{label['id']!r}"
                )
            title_hint = label.get("title")
            if title_hint is not None and label_document.get("title") != title_hint:
                warnings.append(
                    _OperationWarning(
                        check="label-title-hint",
                        message=(
                            f"operation {operation.operationId!r} label title hint is stale for "
                            f"{label['id']!r}: expected {title_hint!r}, found "
                            f"{label_document.get('title')!r}"
                        ),
                        expected=title_hint,
                        found=label_document.get("title"),
                    )
                )
            seen.add(key)
    for dependency_id in set(dependencies):
        _verify_reference(
            kind="dependency",
            reference_id=dependency_id,
            title_hint=None,
            operation_id=operation.operationId,
            reader=reader,
            cache=cache,
            planned_creates=planned_creates,
        )
    return tuple(warnings)


_SUBTASK_ACCEPTABLE_LOSS_FIELDS = {
    plan_name: FIELD_SPECS[plan_name].marvin_name
    for plan_name in (
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
    )
}


def _conversion_value_is_meaningful(value: Any) -> bool:
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None and value != "" and value is not False and value != "unassigned"


def _verify_subtask_sources(
    operation: Operation,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
    planned_creates: dict[str, CreateOperation],
) -> tuple[_OperationWarning, ...]:
    if (
        not isinstance(operation, (UpdateOperation, CreateOperation))
        or operation.target.type == "recurringTask"
    ):
        return ()
    if "subtasks" not in operation.after.model_fields_set or operation.after.subtasks is None:
        return ()
    warnings: list[_OperationWarning] = []
    for subtask in operation.after.subtasks:
        source = subtask.sourceTask
        if source is None:
            continue
        if source.id in planned_creates:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} source task {source.id!r} is newly "
                "created; sourceTask conversion requires an existing loose task"
            )
        if source.id not in cache:
            cache[source.id] = reader.get_doc(source.id)
        document = cache[source.id]
        if document is None or document.get("db") != "Tasks":
            raise LivePreconditionError(
                f"operation {operation.operationId!r} references missing or non-Task "
                f"sourceTask ID {source.id!r}"
            )
        if document.get("title") != source.title:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} sourceTask title is stale for "
                f"{source.id!r}: expected {source.title!r}, found {document.get('title')!r}"
            )
        if document.get("done") is True:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} cannot convert completed sourceTask "
                f"{source.id!r}; its historical completion timestamp is not represented"
            )
        if bool(document.get("done", False)) != subtask.done:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} must preserve sourceTask {source.id!r} "
                f"completion as subtasks[].done={bool(document.get('done', False))!r}"
            )
        reasons = coupled_task_reasons(document)
        if _conversion_value_is_meaningful(document.get("subtasks")):
            reasons.append("existing subtasks")
        if _conversion_value_is_meaningful(document.get("dependsOn")):
            reasons.append("dependencies")
        if any(
            _conversion_value_is_meaningful(document.get(field)) for field in ("times", "duration")
        ):
            reasons.append("tracked time history")
        if reasons:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} cannot convert sourceTask {source.id!r} "
                "with coupled behavior: " + ", ".join(reasons)
            )
        lossy = [
            plan_name
            for plan_name, marvin_name in _SUBTASK_ACCEPTABLE_LOSS_FIELDS.items()
            if _conversion_value_is_meaningful(document.get(marvin_name))
        ]
        accepted = list(source.acceptLoss)
        unacknowledged = [field for field in lossy if field not in accepted]
        unused = [field for field in accepted if field not in lossy]
        if unacknowledged:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} cannot convert sourceTask {source.id!r}; "
                "explicitly acknowledge fields not represented by a basic subtask with "
                "sourceTask.acceptLoss: " + ", ".join(unacknowledged),
                check="source-task-unacknowledged-loss",
                expected=lossy,
                found=accepted,
            )
        if unused:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} sourceTask {source.id!r} accepts loss "
                "that is not present in live state: " + ", ".join(unused),
                check="source-task-unused-loss-acknowledgment",
                expected=lossy,
                found=accepted,
            )
        if accepted:
            warnings.append(
                _OperationWarning(
                    check="source-task-accepted-loss",
                    message=(
                        f"operation {operation.operationId!r} explicitly accepts sourceTask "
                        f"{source.id!r} data loss: " + ", ".join(accepted)
                    ),
                    expected=[],
                    found=accepted,
                )
            )
    return tuple(warnings)


def _verify_project_parent_hierarchy(
    operation: Operation,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
    planned_creates: dict[str, CreateOperation],
) -> None:
    if operation.target.type not in {"project", "category"} or not isinstance(
        operation, (UpdateOperation, CreateOperation)
    ):
        return
    after = operation.after.model_dump(exclude_unset=True, mode="json")
    if "parent" not in after or after["parent"] is None:
        return
    current_id = after["parent"]["id"]
    seen: set[str] = set()
    ancestry: list[str] = []
    while current_id not in {"", "unassigned", "root"}:
        if current_id == operation.target.id:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} would create a project parent cycle"
            )
        if current_id in seen:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} proposed parent hierarchy is already cyclic"
            )
        seen.add(current_id)
        ancestry.append(current_id)

        planned = planned_creates.get(current_id)
        if planned is not None:
            planned_after = planned.after.model_dump(exclude_unset=True, mode="json")
            planned_parent = planned_after.get("parent")
            current_id = planned_parent["id"] if planned_parent is not None else "unassigned"
            continue

        if current_id not in cache:
            cache[current_id] = reader.get_doc(current_id)
        document = cache[current_id]
        if document is None:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} proposed parent ancestry is broken: "
                + " -> ".join(repr(identifier) for identifier in ancestry)
                + " (not found)"
            )
        if document.get("db") != "Categories":
            raise LivePreconditionError(
                f"operation {operation.operationId!r} proposed parent ancestry is broken: "
                + " -> ".join(repr(identifier) for identifier in ancestry)
                + f" (expected a Category document, found db={document.get('db')!r})"
            )
        current_id = document.get("parentId") or "unassigned"


def _verify_container_empty_before_trash(
    operation: Operation,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
) -> None:
    if not isinstance(operation, TrashOperation) or operation.target.type not in {
        "project",
        "category",
    }:
        return
    child_reader = getattr(reader, "get_children", None)
    if callable(child_reader):
        children = child_reader(operation.target.id)
    else:
        # In-memory test/dry-run readers commonly expose their complete document map.
        # The production MarvinClient always uses the documented /children endpoint.
        documents = getattr(reader, "documents", {})
        children = [
            document
            for document in documents.values()
            if document.get("parentId") == operation.target.id
        ]
    remaining = []
    for child in children:
        identifier = child.get("_id")
        if not isinstance(identifier, str):
            continue
        projected = cache.get(identifier, child)
        if (
            projected is not None
            and projected.get("parentId") == operation.target.id
            and not _meaningful(projected.get("deletedAt"))
        ):
            remaining.append(str(projected.get("title") or identifier))
    if remaining:
        preview = ", ".join(remaining[:5])
        suffix = f" (+{len(remaining) - 5} more)" if len(remaining) > 5 else ""
        raise LivePreconditionError(
            f"operation {operation.operationId!r} cannot trash a non-empty "
            f"{operation.target.type}; move or trash every direct child first: {preview}{suffix}"
        )


def _resolved_sibling_order(
    operation: Operation,
    document: dict[str, Any] | None,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
) -> tuple[str, float] | None:
    if not isinstance(operation, UpdateOperation) or operation.siblingOrder is None:
        return None
    assert document is not None
    order = operation.siblingOrder
    after = operation.after.model_dump(exclude_unset=True, mode="json")
    final_day = after.get("scheduledDate", document.get("day"))
    final_parent = (
        (after["parent"]["id"] if after.get("parent") is not None else document.get("parentId"))
        if "parent" in after
        else document.get("parentId")
    )
    uses_rank = operation.target.type in {"project", "category"} or (
        operation.target.type == "task"
        and final_day
        not in {
            None,
            "",
            "unassigned",
        }
    )
    rank_field = "rank" if uses_rank else "masterRank"
    if order.position is not None:
        return rank_field, -1e15 if order.position == "first" else 1e15
    anchor_id = order.beforeId or order.afterId
    assert anchor_id is not None
    if anchor_id == operation.target.id:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} cannot order an item relative to itself"
        )
    if anchor_id not in cache:
        cache[anchor_id] = reader.get_doc(anchor_id)
    anchor = cache[anchor_id]
    if anchor is None or anchor.get("deletedAt") not in {None, ""}:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} sibling-order anchor {anchor_id!r} is absent"
        )
    if operation.target.type == "task" and uses_rank:
        if anchor.get("db") != "Tasks" or anchor.get("day") != final_day:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} sibling-order anchor is not on the same day"
            )
    elif anchor.get("parentId") != final_parent:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} sibling-order anchor is not under the same parent"
        )
    elif operation.target.type in {"project", "category"} and (
        anchor.get("db") != "Categories" or anchor.get("type") != operation.target.type
    ):
        raise LivePreconditionError(
            f"operation {operation.operationId!r} sibling-order anchor is not a sibling "
            f"{operation.target.type}"
        )
    anchor_rank = anchor.get(rank_field)
    if (
        isinstance(anchor_rank, bool)
        or not isinstance(anchor_rank, (int, float))
        or not math.isfinite(anchor_rank)
    ):
        raise LivePreconditionError(
            f"operation {operation.operationId!r} sibling-order anchor has no finite {rank_field}"
        )
    direction = -math.inf if order.beforeId is not None else math.inf
    return rank_field, math.nextafter(float(anchor_rank), direction)


def validate_plan_live(
    plan: ChangePlanV1,
    reader: DocumentReader,
    *,
    now_ms: int,
    strict_concurrency: bool = True,
    selected_indices: tuple[int, ...] | None = None,
    fail_fast: bool = False,
    progress: Callable[[int, int, str], None] | None = None,
) -> LiveValidationResult:
    """Collect live diagnostics for selected 1-based plan entries without mutating Marvin."""

    started = time.perf_counter()
    verify_expected_account(plan, reader)
    cache: dict[str, dict[str, Any] | None] = {}
    metadata_cache: dict[str, Any] = {}
    planned_creates = {
        operation.target.id: operation
        for operation in plan.operations
        if isinstance(operation, CreateOperation)
    }
    checked: list[PreflightOperation] = []
    diagnostics: list[PreflightDiagnostic] = []
    processed_targets: dict[str, str] = {}
    failed_targets: dict[str, str] = {}
    indices = selected_indices or tuple(range(1, len(plan.operations) + 1))
    if not indices or len(indices) != len(set(indices)):
        raise ValueError("selected live-validation indices must be non-empty and unique")
    if any(index < 1 or index > len(plan.operations) for index in indices):
        raise ValueError("selected live-validation index is outside the plan")
    total = len(indices)
    for position, index in enumerate(indices, start=1):
        operation = plan.operations[index - 1]
        if progress is not None:
            progress(position - 1, total, operation.operationId)
        try:
            target_id = operation.target.id
            prior_same_target_operation_id = processed_targets.get(target_id)
            if target_id in failed_targets:
                failed_operation_id = failed_targets[target_id]
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} cannot be checked because prior "
                    f"same-target operation {failed_operation_id!r} failed live validation",
                    check="same-target-predecessor",
                    expected=failed_operation_id,
                    found=None,
                )
            if target_id not in cache:
                cache[target_id] = reader.get_doc(target_id)
            live = cache[target_id]
            if isinstance(operation, CreateOperation):
                if live is not None:
                    raise LivePreconditionError(
                        f"create operation {operation.operationId!r} target ID already exists",
                        check="create-id-available",
                        expected=None,
                        found=live.get("_id"),
                    )
                revision: dict[str, Any] = {}
            else:
                if live is None:
                    raise LivePreconditionError(
                        f"operation {operation.operationId!r} target item does not exist",
                        check="target-exists",
                        expected=target_id,
                        found=None,
                    )
                _verify_recurrence_identity(operation, live, reader, cache)
                _check_existing_preconditions(operation, live)
                revision = revision_snapshot(live)
            if isinstance(operation, CreateOperation):
                _verify_recurrence_identity(operation, live, reader, cache)
            reference_warnings = _verify_references(
                operation, reader, cache, metadata_cache, planned_creates
            )
            source_warnings = _verify_subtask_sources(operation, reader, cache, planned_creates)
            operation_warnings = reference_warnings + source_warnings
            _verify_project_parent_hierarchy(operation, reader, cache, planned_creates)
            _verify_container_empty_before_trash(operation, reader, cache)
            resolved_order = _resolved_sibling_order(operation, live, reader, cache)
            compiled = compile_operation(
                operation,
                live,
                now_ms,
                resolved_sibling_order=resolved_order,
            )
            checked.append(
                PreflightOperation(
                    operation=operation,
                    live_document=live,
                    compiled=compiled,
                    live_revision=revision,
                    prior_same_target_operation_id=prior_same_target_operation_id,
                )
            )
            cache[target_id] = project_compiled_mutation(live, compiled)
            failed_targets.pop(target_id, None)
            diagnostics.extend(
                PreflightDiagnostic(
                    severity="warning",
                    operation_index=index,
                    operation_id=operation.operationId,
                    target_id=target_id,
                    check=warning.check,
                    message=warning.message,
                    expected=warning.expected,
                    found=warning.found,
                )
                for warning in operation_warnings
            )
        except LivePreconditionError as exc:
            failed_targets[operation.target.id] = operation.operationId
            diagnostics.append(
                PreflightDiagnostic(
                    severity="error",
                    operation_index=index,
                    operation_id=operation.operationId,
                    target_id=operation.target.id,
                    check=exc.check,
                    message=str(exc),
                    expected=exc.expected,
                    found=exc.found,
                )
            )
        processed_targets[operation.target.id] = operation.operationId
        if progress is not None:
            progress(position, total, operation.operationId)
        if diagnostics and diagnostics[-1].severity == "error" and fail_fast:
            break
    return LiveValidationResult(
        plan=plan,
        operations=tuple(checked),
        diagnostics=tuple(diagnostics),
        selected_indices=indices,
        checked_at_ms=now_ms,
        strict_concurrency=strict_concurrency,
        unique_documents_checked=len(cache),
        metadata_collections_checked=len(metadata_cache),
        elapsed_ms=round((time.perf_counter() - started) * 1_000),
    )


def preflight_plan(
    plan: ChangePlanV1,
    reader: DocumentReader,
    *,
    now_ms: int,
    strict_concurrency: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> PreflightResult:
    """Read every operation, report all violations together, and compile a clean plan."""

    validation = validate_plan_live(
        plan,
        reader,
        now_ms=now_ms,
        strict_concurrency=strict_concurrency,
        progress=progress,
    )
    if validation.errors:
        if len(validation.errors) == 1:
            message = validation.errors[0].message
        else:
            details = "\n".join(
                f"  {item.operation_index}. [{item.operation_id}] {item.message}"
                for item in validation.errors
            )
            message = (
                f"live preflight found {len(validation.errors)} errors across "
                f"{len(validation.selected_indices)} operations:\n{details}"
            )
        raise LivePreconditionError(message, check="live-validation")
    return PreflightResult(
        plan=plan,
        operations=validation.operations,
        checked_at_ms=now_ms,
        strict_concurrency=strict_concurrency,
        warnings=validation.warnings,
        unique_documents_checked=validation.unique_documents_checked,
        metadata_collections_checked=validation.metadata_collections_checked,
        elapsed_ms=validation.elapsed_ms,
    )


def recheck_operation(
    checked: PreflightOperation,
    reader: DocumentReader,
    *,
    strict_concurrency: bool,
    expected_revision: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Repeat target checks immediately before a write and detect intervening edits."""

    operation = checked.operation
    current = reader.get_doc(operation.target.id)
    if isinstance(operation, CreateOperation):
        if current is not None:
            raise LivePreconditionError(
                f"create operation {operation.operationId!r} target appeared after preflight"
            )
        return None
    if current is None:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} target disappeared after preflight"
        )
    _check_existing_preconditions(operation, current)
    if strict_concurrency:
        revision = revision_snapshot(current)
        checked_revision = expected_revision or checked.live_revision
        for field in ("_rev", "updatedAt"):
            before = checked_revision[field]
            after = revision[field]
            if before["present"] and before != after:
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} {field} changed after preflight"
                )
    return current


def desired_fields_match(document: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    """Verify exact top-level storage values after a mutation or ambiguous timeout."""

    if document is None:
        return False
    return all(field in document and document[field] == value for field, value in desired.items())
