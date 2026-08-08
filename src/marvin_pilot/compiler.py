"""Pure compilation from reviewed operations to exact Marvin API payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from marvin_pilot.field_registry import FIELD_SPECS, compile_plan_field, field_snapshot
from marvin_pilot.models.plan_v1 import CreateOperation, TrashOperation, UpdateOperation


@dataclass(frozen=True, slots=True)
class CompiledMutation:
    operation_id: str
    action: str
    target_id: str
    endpoint: str
    payload: dict[str, Any]
    desired_fields: dict[str, Any]
    before_fields: dict[str, dict[str, Any]]


def _setters_for_fields(
    fields: dict[str, Any], now_ms: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    setters: list[dict[str, Any]] = []
    desired: dict[str, Any] = {}
    for plan_field, plan_value in fields.items():
        marvin_field, marvin_value = compile_plan_field(plan_field, plan_value)
        setters.extend(
            [
                {"key": marvin_field, "val": marvin_value},
                {"key": f"fieldUpdates.{marvin_field}", "val": now_ms},
            ]
        )
        desired[marvin_field] = marvin_value
    return setters, desired


def compile_update(
    operation: UpdateOperation, live_document: dict[str, Any], now_ms: int
) -> CompiledMutation:
    """Compile one task update, including first-scheduling metadata when necessary."""

    after = operation.after.model_dump(exclude_unset=True, mode="json")
    setters, desired = _setters_for_fields(after, now_ms)

    if "scheduledDate" in after:
        new_day = after["scheduledDate"]
        first_scheduled = live_document.get("firstScheduled")
        if new_day is not None and first_scheduled in {None, "", "unassigned"}:
            setters.extend(
                [
                    {"key": "firstScheduled", "val": new_day},
                    {"key": "fieldUpdates.firstScheduled", "val": now_ms},
                ]
            )
            desired["firstScheduled"] = new_day

    setters.append({"key": "updatedAt", "val": now_ms})
    touched = {field: field_snapshot(live_document, field) for field in desired}
    return CompiledMutation(
        operation_id=operation.operationId,
        action="update",
        target_id=operation.target.id,
        endpoint="doc/update",
        payload={"itemId": operation.target.id, "setters": setters},
        desired_fields=desired,
        before_fields=touched,
    )


def compile_create(operation: CreateOperation, now_ms: int) -> CompiledMutation:
    """Compile a complete minimal Tasks document with a caller-generated UUID."""

    after = operation.after.model_dump(exclude_unset=True, mode="json")
    document: dict[str, Any] = {
        "_id": operation.target.id,
        "db": "Tasks",
        "done": False,
        "day": "unassigned",
        "parentId": "unassigned",
        "createdAt": now_ms,
        "updatedAt": now_ms,
    }
    field_updates: dict[str, int] = {}
    for plan_field, plan_value in after.items():
        marvin_field, marvin_value = compile_plan_field(plan_field, plan_value)
        document[marvin_field] = marvin_value
        field_updates[marvin_field] = now_ms
    if after.get("scheduledDate") is not None:
        document["firstScheduled"] = after["scheduledDate"]
        field_updates["firstScheduled"] = now_ms
    document["fieldUpdates"] = field_updates
    desired = {
        key: value for key, value in document.items() if key not in {"fieldUpdates", "updatedAt"}
    }
    return CompiledMutation(
        operation_id=operation.operationId,
        action="create",
        target_id=operation.target.id,
        endpoint="doc/create",
        payload=document,
        desired_fields=desired,
        before_fields={},
    )


def compile_trash(
    operation: TrashOperation, live_document: dict[str, Any], now_ms: int
) -> CompiledMutation:
    """Compile the documented portion of Marvin's reversible Trash transition."""

    desired = {"deletedAt": now_ms}
    setters = [
        {"key": "deletedAt", "val": now_ms},
        {"key": "fieldUpdates.deletedAt", "val": now_ms},
        {"key": "updatedAt", "val": now_ms},
    ]
    before = {field: field_snapshot(live_document, field) for field in ("deletedAt", "restoredAt")}
    return CompiledMutation(
        operation_id=operation.operationId,
        action="trash",
        target_id=operation.target.id,
        endpoint="doc/update",
        payload={"itemId": operation.target.id, "setters": setters},
        desired_fields=desired,
        before_fields=before,
    )


def compile_operation(
    operation: UpdateOperation | CreateOperation | TrashOperation,
    live_document: dict[str, Any] | None,
    now_ms: int,
) -> CompiledMutation:
    """Dispatch one discriminated operation to its pure compiler."""

    if isinstance(operation, CreateOperation):
        return compile_create(operation, now_ms)
    if live_document is None:
        raise ValueError("existing-task compilation requires a live document")
    if isinstance(operation, UpdateOperation):
        return compile_update(operation, live_document, now_ms)
    return compile_trash(operation, live_document, now_ms)


def affected_marvin_fields(operation: UpdateOperation) -> list[str]:
    """Return the normal top-level Marvin fields selected by an update."""

    return [
        spec.marvin_name
        for field, spec in FIELD_SPECS.items()
        if field in operation.after.model_fields_set
    ]
