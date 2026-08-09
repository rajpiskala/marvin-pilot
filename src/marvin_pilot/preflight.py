"""Whole-plan live validation before the first Marvin mutation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from marvin_pilot.compiler import CompiledMutation, compile_operation
from marvin_pilot.errors import LivePreconditionError
from marvin_pilot.field_registry import FIELD_SPECS, live_field_matches
from marvin_pilot.models.plan_v1 import (
    ChangePlanV1,
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


def coupled_task_reasons(document: dict[str, Any]) -> list[str]:
    """Return coupled behaviors that make a simple document mutation unsafe in v1."""

    reasons: list[str] = []
    if any(_meaningful(document.get(field)) for field in ("recurring", "echo")):
        reasons.append("recurrence/echo")
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


def _check_existing_preconditions(
    operation: UpdateOperation | TrashOperation,
    document: dict[str, Any],
) -> None:
    if document.get("db") != "Tasks":
        raise LivePreconditionError(
            f"operation {operation.operationId!r} targets a non-Task document"
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
    reasons = coupled_task_reasons(document)
    if reasons:
        raise LivePreconditionError(
            f"operation {operation.operationId!r} is blocked by coupled behavior: "
            + ", ".join(reasons)
        )
    if _meaningful(document.get("deletedAt")):
        if isinstance(operation, TrashOperation):
            raise LivePreconditionError(
                f"operation {operation.operationId!r} targets a task already in Trash"
            )
        raise LivePreconditionError(
            f"operation {operation.operationId!r} targets a task currently in Trash"
        )
    if isinstance(operation, UpdateOperation):
        mismatches = []
        before = operation.before.model_dump(exclude_unset=True, mode="json")
        for field, expected in before.items():
            if not live_field_matches(field, expected, document):
                marvin_field = FIELD_SPECS[field].marvin_name
                mismatches.append(
                    f"{field} expected {expected!r}, live {document.get(marvin_field)!r}"
                )
        if mismatches:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} has stale fields: " + "; ".join(mismatches)
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
) -> None:
    if kind == "parent" and reference_id == "unassigned":
        if title_hint is not None and title_hint != "Inbox":
            raise LivePreconditionError(
                f"operation {operation_id!r} calls parent 'unassigned' {title_hint!r}, not 'Inbox'"
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
        )


def preflight_plan(
    plan: ChangePlanV1,
    reader: DocumentReader,
    *,
    now_ms: int,
    strict_concurrency: bool = True,
) -> PreflightResult:
    """Read and validate every target/reference, then compile every operation."""

    cache: dict[str, dict[str, Any] | None] = {}
    metadata_cache: dict[str, Any] = {}
    checked: list[PreflightOperation] = []
    for operation in plan.operations:
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
                    f"operation {operation.operationId!r} target task does not exist"
                )
            _check_existing_preconditions(operation, live)
            revision = _revision_snapshot(live)
        _verify_references(operation, reader, cache, metadata_cache)
        checked.append(
            PreflightOperation(
                operation=operation,
                live_document=live,
                compiled=compile_operation(operation, live, now_ms),
                live_revision=revision,
            )
        )
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
