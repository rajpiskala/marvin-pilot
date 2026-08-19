"""Whole-plan live validation before the first Marvin mutation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from marvin_pilot.compiler import CompiledMutation, compile_operation
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


@dataclass(frozen=True, slots=True)
class PreflightOperation:
    operation: Operation
    live_document: dict[str, Any] | None
    compiled: CompiledMutation
    live_revision: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreflightResult:
    plan: ChangePlanV1
    operations: tuple[PreflightOperation, ...]
    checked_at_ms: int
    strict_concurrency: bool


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
        reasons.append("reminder server state")
    if any(_meaningful(document.get(field)) for field in ("calId", "calURL", "etag", "calData")):
        reasons.append("calendar synchronization")
    return reasons


def _revision_snapshot(document: dict[str, Any]) -> dict[str, Any]:
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
        "recurringTask": "RecurringTasks",
    }[operation.target.type]
    if (
        document.get("db") != expected_db
        or (operation.target.type == "project" and document.get("type") != "project")
        or (operation.target.type == "recurringTask" and document.get("recurringType") != "task")
    ):
        document_kind = {
            "task": "a non-Task",
            "project": "not a live project",
            "recurringTask": "not a live recurring-task series",
        }[operation.target.type]
        raise LivePreconditionError(
            f"operation {operation.operationId!r} target is {document_kind} document"
        )
    if document.get("title") != operation.target.title:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} title is stale: expected "
            f"{operation.target.title!r}, found {document.get('title')!r}"
        )
    if (
        operation.expectedUpdatedAt is not None
        and document.get("updatedAt") != operation.expectedUpdatedAt
    ):
        raise LivePreconditionError(
            f"operation {operation.operationId!r} updatedAt is stale: expected "
            f"{operation.expectedUpdatedAt}, found {document.get('updatedAt')!r}"
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
    if isinstance(operation, CompleteOperation) and document.get("done") is True:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} targets an item already completed"
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
            f"{recurrence.seriesId!r}, found {document.get('recurringTaskId')!r}"
        )
    if document.get("day") != recurrence.scheduledDate:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} occurrence date is stale: expected "
            f"{recurrence.scheduledDate!r}, found {document.get('day')!r}"
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
            f"{recurrence.seriesTitle!r}, found {template.get('title')!r}"
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
) -> None:
    if kind == "parent" and reference_id == "unassigned":
        if title_hint is not None and title_hint != "Inbox":
            raise LivePreconditionError(
                f"operation {operation_id!r} calls parent 'unassigned' {title_hint!r}, not 'Inbox'"
            )
        return
    planned = planned_creates.get(reference_id)
    if planned is not None:
        if kind == "parent" and planned.target.type != "project":
            raise LivePreconditionError(
                f"operation {operation_id!r} references newly created non-project parent "
                f"{reference_id!r}"
            )
        planned_title = planned.after.title
        if title_hint is not None and planned_title != title_hint:
            raise LivePreconditionError(
                f"operation {operation_id!r} {kind} title is stale for planned create "
                f"{reference_id!r}: expected {title_hint!r}, found {planned_title!r}"
            )
        return
    if reference_id not in cache:
        cache[reference_id] = reader.get_doc(reference_id)
    document = cache[reference_id]
    if document is None:
        raise LivePreconditionError(
            f"operation {operation_id!r} references missing {kind} ID {reference_id!r}"
        )
    if title_hint is not None and document.get("title") != title_hint:
        raise LivePreconditionError(
            f"operation {operation_id!r} {kind} title is stale for {reference_id!r}: "
            f"expected {title_hint!r}, found {document.get('title')!r}"
        )


def _verify_references(
    operation: Operation,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
    metadata_cache: dict[str, Any],
    planned_creates: dict[str, CreateOperation],
) -> None:
    parents, labels, dependencies = _reference_hints(operation)
    seen: set[tuple[str, str, str | None]] = set()
    for parent in parents:
        key = ("parent", parent["id"], parent.get("title"))
        if key not in seen:
            _verify_reference(
                kind="parent",
                reference_id=parent["id"],
                title_hint=parent.get("title"),
                operation_id=operation.operationId,
                reader=reader,
                cache=cache,
                planned_creates=planned_creates,
            )
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
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} label title is stale for "
                    f"{label['id']!r}: expected {title_hint!r}, found "
                    f"{label_document.get('title')!r}"
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


_SUBTASK_CONVERSION_LOSS_FIELDS = tuple(
    dict.fromkeys(
        [
            spec.marvin_name
            for plan_name, spec in FIELD_SPECS.items()
            if plan_name not in {"title", "parent"}
        ]
        + ["firstScheduled", "times", "duration", "workedOnAt"]
    )
)


def _conversion_value_is_meaningful(value: Any) -> bool:
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None and value != "" and value is not False and value != "unassigned"


def _verify_subtask_sources(
    operation: Operation,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
    planned_creates: dict[str, CreateOperation],
) -> None:
    if (
        not isinstance(operation, (UpdateOperation, CreateOperation))
        or operation.target.type == "recurringTask"
    ):
        return
    if "subtasks" not in operation.after.model_fields_set or operation.after.subtasks is None:
        return
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
        if reasons:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} cannot convert sourceTask {source.id!r} "
                "with coupled behavior: " + ", ".join(reasons)
            )
        lossy = [
            field
            for field in _SUBTASK_CONVERSION_LOSS_FIELDS
            if _conversion_value_is_meaningful(document.get(field))
        ]
        if lossy:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} cannot convert sourceTask {source.id!r}; "
                "these fields are not represented by a basic subtask: " + ", ".join(lossy)
            )


def _verify_project_parent_hierarchy(
    operation: Operation,
    reader: DocumentReader,
    cache: dict[str, dict[str, Any] | None],
    planned_creates: dict[str, CreateOperation],
) -> None:
    if operation.target.type != "project" or not isinstance(
        operation, (UpdateOperation, CreateOperation)
    ):
        return
    after = operation.after.model_dump(exclude_unset=True, mode="json")
    if "parent" not in after or after["parent"] is None:
        return
    current_id = after["parent"]["id"]
    seen: set[str] = set()
    while current_id not in {"", "unassigned"}:
        if current_id == operation.target.id:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} would create a project parent cycle"
            )
        if current_id in seen:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} proposed parent hierarchy is already cyclic"
            )
        seen.add(current_id)

        planned = planned_creates.get(current_id)
        if planned is not None:
            planned_after = planned.after.model_dump(exclude_unset=True, mode="json")
            planned_parent = planned_after.get("parent")
            current_id = planned_parent["id"] if planned_parent is not None else "unassigned"
            continue

        if current_id not in cache:
            cache[current_id] = reader.get_doc(current_id)
        document = cache[current_id]
        if document is None or document.get("db") != "Categories":
            raise LivePreconditionError(
                f"operation {operation.operationId!r} proposed parent ancestry contains a "
                f"missing or non-Category document {current_id!r}"
            )
        current_id = document.get("parentId") or "unassigned"


def preflight_plan(
    plan: ChangePlanV1,
    reader: DocumentReader,
    *,
    now_ms: int,
    strict_concurrency: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> PreflightResult:
    """Read and validate every target/reference, then compile every operation."""

    cache: dict[str, dict[str, Any] | None] = {}
    metadata_cache: dict[str, Any] = {}
    planned_creates = {
        operation.target.id: operation
        for operation in plan.operations
        if isinstance(operation, CreateOperation)
    }
    checked: list[PreflightOperation] = []
    total = len(plan.operations)
    for index, operation in enumerate(plan.operations, start=1):
        if progress is not None:
            progress(index - 1, total, operation.operationId)
        target_id = operation.target.id
        if target_id not in cache:
            cache[target_id] = reader.get_doc(target_id)
        live = cache[target_id]
        if isinstance(operation, CreateOperation):
            if live is not None:
                raise LivePreconditionError(
                    f"create operation {operation.operationId!r} target ID already exists"
                )
            revision: dict[str, Any] = {}
        else:
            if live is None:
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} target item does not exist"
                )
            _verify_recurrence_identity(operation, live, reader, cache)
            _check_existing_preconditions(operation, live)
            revision = _revision_snapshot(live)
        if isinstance(operation, CreateOperation):
            _verify_recurrence_identity(operation, live, reader, cache)
        _verify_references(operation, reader, cache, metadata_cache, planned_creates)
        _verify_subtask_sources(operation, reader, cache, planned_creates)
        _verify_project_parent_hierarchy(operation, reader, cache, planned_creates)
        checked.append(
            PreflightOperation(
                operation=operation,
                live_document=live,
                compiled=compile_operation(operation, live, now_ms),
                live_revision=revision,
            )
        )
        if progress is not None:
            progress(index, total, operation.operationId)
    return PreflightResult(
        plan=plan,
        operations=tuple(checked),
        checked_at_ms=now_ms,
        strict_concurrency=strict_concurrency,
    )


def recheck_operation(
    checked: PreflightOperation,
    reader: DocumentReader,
    *,
    strict_concurrency: bool,
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
        revision = _revision_snapshot(current)
        for field in ("_rev", "updatedAt"):
            before = checked.live_revision[field]
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
