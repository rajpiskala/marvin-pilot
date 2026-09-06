"""Pure compilation from reviewed operations to exact Marvin API payloads."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from marvin_pilot.completion import completion_local_date
from marvin_pilot.field_registry import (
    FIELD_SPECS,
    compile_fields_for_target,
    compile_plan_field,
    compile_recurring_task_field,
    field_snapshot,
)
from marvin_pilot.models.plan_v1 import (
    CompleteOperation,
    CreateOperation,
    TrashOperation,
    UpdateOperation,
)


@dataclass(frozen=True, slots=True)
class CompiledMutation:
    operation_id: str
    action: str
    target_id: str
    endpoint: str
    payload: dict[str, Any]
    desired_fields: dict[str, Any]
    before_fields: dict[str, dict[str, Any]]


def _set_nested_value(document: dict[str, Any], path: str, value: Any) -> None:
    current = document
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = deepcopy(value)


def project_compiled_mutation(
    document: dict[str, Any] | None,
    compiled: CompiledMutation,
) -> dict[str, Any] | None:
    """Project one reviewed mutation without guessing Marvin's next CouchDB revision."""

    if compiled.endpoint == "doc/delete":
        return None
    if compiled.endpoint == "doc/create":
        return deepcopy(compiled.payload)
    if document is None:
        raise ValueError("document update projection requires an existing document")
    projected = deepcopy(document)
    for setter in compiled.payload["setters"]:
        _set_nested_value(projected, setter["key"], setter["val"])
    return projected


def _setters_for_fields(
    fields: dict[str, Any],
    now_ms: int,
    *,
    target_type: str,
    live_document: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    setters: list[dict[str, Any]] = []
    desired: dict[str, Any] = {}
    for plan_field, plan_value in fields.items():
        if target_type == "recurringTask":
            compiled_fields = compile_recurring_task_field(plan_field, plan_value)
        else:
            marvin_field, marvin_value = compile_plan_field(plan_field, plan_value)
            compiled_fields = {marvin_field: marvin_value}
        if plan_field == "subtasks" and target_type != "recurringTask":
            live_subtasks = live_document.get("subtasks") if live_document is not None else None
            compiled_fields = {
                "subtasks": _merge_subtasks(plan_value, live_subtasks, now_ms=now_ms)
            }
        if target_type == "project" and plan_field == "scheduledDate" and plan_value is None:
            compiled_fields = {"day": None}
        for marvin_field, marvin_value in compiled_fields.items():
            setters.extend(
                [
                    {"key": marvin_field, "val": marvin_value},
                    {"key": f"fieldUpdates.{marvin_field}", "val": now_ms},
                ]
            )
            desired[marvin_field] = marvin_value
    return setters, desired


def _merge_subtasks(
    planned: list[dict[str, Any]] | None,
    live_value: Any,
    *,
    now_ms: int,
) -> dict[str, dict[str, Any]]:
    """Merge the typed subtask subset while preserving unedited native metadata by ID."""

    if planned is None:
        return {}
    live = live_value if isinstance(live_value, dict) else {}
    result: dict[str, dict[str, Any]] = {}
    for rank, item in enumerate(planned, start=1):
        identifier = item["id"]
        previous = live.get(identifier)
        native = dict(previous) if isinstance(previous, dict) else {}
        was_done = bool(native.get("done", False))
        is_done = bool(item.get("done", False))
        native.update(
            {
                "_id": identifier,
                "title": item["title"],
                "rank": rank,
                "done": is_done,
            }
        )
        if is_done and not was_done:
            native["doneAt"] = now_ms
        elif not is_done:
            native.pop("doneAt", None)
        result[identifier] = native
    return result


def compile_update(
    operation: UpdateOperation,
    live_document: dict[str, Any],
    now_ms: int,
    *,
    resolved_sibling_order: tuple[str, float] | None = None,
) -> CompiledMutation:
    """Compile one item update, including first-scheduling metadata when necessary."""

    after = operation.after.model_dump(exclude_unset=True, mode="json")
    setters, desired = _setters_for_fields(
        after,
        now_ms,
        target_type=operation.target.type,
        live_document=live_document,
    )

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

    if resolved_sibling_order is not None:
        rank_field, rank_value = resolved_sibling_order
        setters.extend(
            [
                {"key": rank_field, "val": rank_value},
                {"key": f"fieldUpdates.{rank_field}", "val": now_ms},
            ]
        )
        desired[rank_field] = rank_value

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
    """Compile a complete minimal task or project document with a caller-generated UUID."""

    after = operation.after.model_dump(exclude_unset=True, mode="json")
    if operation.target.type == "recurringTask":
        document: dict[str, Any] = {
            "_id": operation.target.id,
            "db": "RecurringTasks",
            "recurringType": "task",
            "parentId": "unassigned",
            "type": "daily",
            "day": 0,
            "date": 1,
            "weekDays": [0],
            "repeat": 1,
            "repeatStart": after["cadence"]["startDate"],
            "limitToWeekdays": False,
            "subtaskList": [],
            "sectionId": None,
            "timeEstimate": 0,
            "labelIds": [],
            "dueIn": None,
            "echoDays": 1,
            "onCount": 7,
            "offCount": 7,
            "customRecurrence": "",
            "endDate": None,
            "rank": 0,
            "note": "",
            "isStarred": False,
            "isFrogged": False,
            "isReward": False,
            "rewardPoints": 0,
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        compiled_after = compile_fields_for_target("recurringTask", after)
        document.update(compiled_after)
        document["fieldUpdates"] = dict.fromkeys(compiled_after, now_ms)
        desired = {
            key: value
            for key, value in document.items()
            if key not in {"fieldUpdates", "updatedAt"}
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

    is_project = operation.target.type == "project"
    is_container = operation.target.type in {"project", "category"}
    document: dict[str, Any] = {
        "_id": operation.target.id,
        "db": "Categories" if is_container else "Tasks",
        "done": False,
        "day": None if is_project else "unassigned",
        "parentId": "unassigned",
        "createdAt": now_ms,
        "updatedAt": now_ms,
    }
    if is_container:
        document["type"] = operation.target.type
    field_updates: dict[str, int] = {}
    for plan_field, plan_value in after.items():
        marvin_field, marvin_value = compile_plan_field(plan_field, plan_value)
        if plan_field == "subtasks":
            marvin_value = _merge_subtasks(plan_value, None, now_ms=now_ms)
        if is_project and plan_field == "scheduledDate" and plan_value is None:
            marvin_value = None
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


def compile_complete(
    operation: CompleteOperation, live_document: dict[str, Any], now_ms: int
) -> CompiledMutation:
    """Compile a backdate-capable completion for a task or project."""

    candidate = (
        operation.completedAt[:-1] + "+00:00"
        if operation.completedAt.endswith("Z")
        else operation.completedAt
    )
    completed = datetime.fromisoformat(candidate)
    completed_ms = int(completed.timestamp() * 1_000)
    completion_day = completion_local_date(operation.completedAt)
    desired: dict[str, Any] = {
        "done": True,
        "doneAt": completed_ms,
        "day": completion_day,
    }
    setters = [
        {"key": "done", "val": True},
        {"key": "fieldUpdates.done", "val": now_ms},
        {"key": "doneAt", "val": completed_ms},
        {"key": "fieldUpdates.doneAt", "val": now_ms},
        {"key": "day", "val": completion_day},
        {"key": "fieldUpdates.day", "val": now_ms},
    ]
    touched_fields = ["done", "doneAt", "day"]
    if operation.target.type == "project":
        done_date = completion_day
        desired["doneDate"] = done_date
        setters.extend(
            [
                {"key": "doneDate", "val": done_date},
                {"key": "fieldUpdates.doneDate", "val": now_ms},
            ]
        )
        touched_fields.append("doneDate")
    setters.append({"key": "updatedAt", "val": now_ms})
    return CompiledMutation(
        operation_id=operation.operationId,
        action="complete",
        target_id=operation.target.id,
        endpoint="doc/update",
        payload={"itemId": operation.target.id, "setters": setters},
        desired_fields=desired,
        before_fields={field: field_snapshot(live_document, field) for field in touched_fields},
    )


def compile_trash(
    operation: TrashOperation, live_document: dict[str, Any], now_ms: int
) -> CompiledMutation:
    """Compile a Pilot-managed deletion recovered from the durable apply receipt."""

    return CompiledMutation(
        operation_id=operation.operationId,
        action="trash",
        target_id=operation.target.id,
        endpoint="doc/delete",
        payload={"itemId": operation.target.id},
        desired_fields={},
        before_fields={},
    )


def compile_operation(
    operation: UpdateOperation | CreateOperation | TrashOperation | CompleteOperation,
    live_document: dict[str, Any] | None,
    now_ms: int,
    *,
    resolved_sibling_order: tuple[str, float] | None = None,
) -> CompiledMutation:
    """Dispatch one discriminated operation to its pure compiler."""

    if isinstance(operation, CreateOperation):
        return compile_create(operation, now_ms)
    if live_document is None:
        raise ValueError("existing-item compilation requires a live document")
    if isinstance(operation, UpdateOperation):
        return compile_update(
            operation,
            live_document,
            now_ms,
            resolved_sibling_order=resolved_sibling_order,
        )
    if isinstance(operation, CompleteOperation):
        return compile_complete(operation, live_document, now_ms)
    return compile_trash(operation, live_document, now_ms)


def affected_marvin_fields(operation: UpdateOperation) -> list[str]:
    """Return the normal top-level Marvin fields selected by an update."""

    if operation.target.type == "recurringTask":
        result = []
        after = operation.after.model_dump(exclude_unset=True, mode="json")
        for field in operation.after.model_fields_set:
            result.extend(compile_recurring_task_field(field, after[field]))
        return result
    return [
        spec.marvin_name
        for field, spec in FIELD_SPECS.items()
        if field in operation.after.model_fields_set
    ]
