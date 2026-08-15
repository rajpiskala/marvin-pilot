from __future__ import annotations

import json

from marvin_pilot.compiler import (
    affected_marvin_fields,
    compile_create,
    compile_operation,
    compile_trash,
    compile_update,
)
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.models.plan_v1 import CreateOperation, TrashOperation, UpdateOperation
from marvin_pilot.plan_io import parse_plan_bytes

NOW_MS = 1_786_233_600_123


def operations():
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    return plan.operations


def setter_map(payload: dict) -> dict:
    return {setter["key"]: setter["val"] for setter in payload["setters"]}


def test_update_compiles_values_field_metadata_and_one_timestamp() -> None:
    operation = operations()[1]
    assert isinstance(operation, UpdateOperation)
    live = {
        "_id": operation.target.id,
        "title": "Eat dinner with Jacob",
        "parentId": "unassigned",
        "timeEstimate": None,
        "firstScheduled": "2026-07-01",
    }
    compiled = compile_update(operation, live, NOW_MS)
    setters = setter_map(compiled.payload)

    assert compiled.endpoint == "doc/update"
    assert compiled.payload["itemId"] == operation.target.id
    assert setters["title"].startswith("5:00pm")
    assert setters["parentId"] == "people-category-id"
    assert setters["timeEstimate"] == 4 * 3_600_000 + 30 * 60_000
    assert setters["fieldUpdates.title"] == NOW_MS
    assert setters["fieldUpdates.parentId"] == NOW_MS
    assert setters["fieldUpdates.timeEstimate"] == NOW_MS
    assert setters["updatedAt"] == NOW_MS
    assert "duration" not in setters
    assert compiled.before_fields["parentId"] == {"present": True, "value": "unassigned"}


def test_first_schedule_sets_first_scheduled() -> None:
    operation = operations()[0]
    assert isinstance(operation, UpdateOperation)
    compiled = compile_update(operation, {"day": "2026-08-08"}, NOW_MS)
    setters = setter_map(compiled.payload)
    assert setters["day"] == "2026-08-09"
    assert setters["firstScheduled"] == "2026-08-09"
    assert setters["fieldUpdates.firstScheduled"] == NOW_MS
    assert compiled.before_fields["firstScheduled"] == {"present": False}


def test_reschedule_preserves_existing_first_scheduled() -> None:
    operation = operations()[0]
    compiled = compile_update(
        operation, {"day": "2026-08-08", "firstScheduled": "2026-07-01"}, NOW_MS
    )
    assert "firstScheduled" not in setter_map(compiled.payload)


def test_unschedule_maps_to_unassigned_without_changing_first_scheduled() -> None:
    value = json.loads(json.dumps(EXAMPLE_PLAN))
    value["operations"] = [value["operations"][0]]
    value["operations"][0]["after"]["scheduledDate"] = None
    operation = parse_plan_bytes(json.dumps(value).encode()).operations[0]
    compiled = compile_update(operation, {"day": "2026-08-08"}, NOW_MS)
    setters = setter_map(compiled.payload)
    assert setters["day"] == "unassigned"
    assert "firstScheduled" not in setters


def test_create_compiles_minimal_task_shape_and_defaults() -> None:
    operation = operations()[2]
    assert isinstance(operation, CreateOperation)
    compiled = compile_create(operation, NOW_MS)
    document = compiled.payload
    assert compiled.endpoint == "doc/create"
    assert document["_id"] == operation.target.id
    assert document["db"] == "Tasks"
    assert document["done"] is False
    assert document["parentId"] == "unassigned"
    assert document["day"] == "2026-08-08"
    assert document["firstScheduled"] == "2026-08-08"
    assert document["timeEstimate"] == 3_600_000
    assert document["createdAt"] == NOW_MS
    assert document["updatedAt"] == NOW_MS
    assert set(document["fieldUpdates"]) >= {
        "title",
        "parentId",
        "day",
        "timeEstimate",
        "firstScheduled",
    }
    assert "updatedAt" not in compiled.desired_fields


def test_create_without_schedule_stays_unassigned() -> None:
    value = json.loads(json.dumps(EXAMPLE_PLAN))
    value["operations"] = [value["operations"][2]]
    del value["operations"][0]["after"]["scheduledDate"]
    operation = parse_plan_bytes(json.dumps(value).encode()).operations[0]
    compiled = compile_create(operation, NOW_MS)
    assert compiled.payload["day"] == "unassigned"
    assert "firstScheduled" not in compiled.payload


def test_trash_is_reversible_update_never_permanent_delete() -> None:
    operation = operations()[3]
    assert isinstance(operation, TrashOperation)
    compiled = compile_trash(operation, {"title": "Study chapter 3", "restoredAt": 123}, NOW_MS)
    setters = setter_map(compiled.payload)
    assert compiled.endpoint == "doc/update"
    assert setters == {
        "deletedAt": NOW_MS,
        "fieldUpdates.deletedAt": NOW_MS,
        "updatedAt": NOW_MS,
    }
    assert compiled.before_fields == {
        "deletedAt": {"present": False},
        "restoredAt": {"present": True, "value": 123},
    }


def test_dispatch_and_affected_fields() -> None:
    update, _, create, trash = operations()
    assert compile_operation(update, {"firstScheduled": "2026-01-01"}, NOW_MS).action == "update"
    assert compile_operation(create, None, NOW_MS).action == "create"
    assert compile_operation(trash, {}, NOW_MS).action == "trash"
    assert affected_marvin_fields(update) == ["day"]


def test_display_metadata_never_compiles_to_marvin() -> None:
    update, _, create, trash = operations()
    compiled = [
        compile_operation(update, {"day": "2026-08-08"}, NOW_MS),
        compile_operation(create, None, NOW_MS),
        compile_operation(trash, {}, NOW_MS),
    ]
    serialized = json.dumps([mutation.payload for mutation in compiled])
    assert "display" not in serialized
    assert "beforeSection" not in serialized
    assert "afterSection" not in serialized


def test_subtask_update_preserves_native_metadata_and_applies_exact_order() -> None:
    value = {
        "schemaVersion": 1,
        "planId": "11111111-1111-4111-8111-111111111111",
        "createdAt": "2026-08-14T08:00:00-07:00",
        "summary": "Edit ordered subtasks.",
        "operations": [
            {
                "operationId": "edit-subtasks",
                "action": "update",
                "target": {"type": "task", "id": "parent-task", "title": "Handle dinner"},
                "reason": "Make the dinner sequence explicit.",
                "before": {
                    "subtasks": [
                        {"id": "sub-a", "title": "Order food", "done": False},
                        {"id": "sub-b", "title": "Pick up food", "done": True},
                        {"id": "sub-remove", "title": "Obsolete", "done": False},
                    ]
                },
                "after": {
                    "subtasks": [
                        {"id": "sub-b", "title": "Pick up food", "done": True},
                        {"id": "sub-a", "title": "Order food now", "done": True},
                        {"id": "sub-c", "title": "Check the food", "done": False},
                    ]
                },
            }
        ],
    }
    operation = parse_plan_bytes(json.dumps(value).encode()).operations[0]
    live = {
        "subtasks": {
            "sub-a": {
                "_id": "sub-a",
                "title": "Order food",
                "rank": 10,
                "done": False,
                "nativeExtension": {"keep": True},
            },
            "sub-b": {
                "_id": "sub-b",
                "title": "Pick up food",
                "rank": 20,
                "done": True,
                "doneAt": 123,
            },
            "sub-remove": {
                "_id": "sub-remove",
                "title": "Obsolete",
                "rank": 30,
                "done": False,
            },
        }
    }

    compiled = compile_update(operation, live, NOW_MS)
    subtasks = setter_map(compiled.payload)["subtasks"]

    assert list(subtasks) == ["sub-b", "sub-a", "sub-c"]
    assert [item["rank"] for item in subtasks.values()] == [1, 2, 3]
    assert subtasks["sub-b"]["doneAt"] == 123
    assert subtasks["sub-a"]["doneAt"] == NOW_MS
    assert subtasks["sub-a"]["nativeExtension"] == {"keep": True}
    assert "sub-remove" not in subtasks
    assert compiled.before_fields["subtasks"]["value"] == live["subtasks"]


def test_create_compiles_ordered_subtasks_without_source_provenance() -> None:
    value = {
        "schemaVersion": 1,
        "planId": "22222222-2222-4222-8222-222222222222",
        "createdAt": "2026-08-14T08:00:00-07:00",
        "summary": "Create a task with subtasks.",
        "operations": [
            {
                "operationId": "create-dinner",
                "action": "create",
                "target": {
                    "type": "task",
                    "id": "33333333-3333-4333-8333-333333333333",
                },
                "reason": "Represent the workflow as one task.",
                "after": {
                    "title": "Handle dinner",
                    "subtasks": [
                        {"id": "sub-1", "title": "Order food"},
                        {"id": "sub-2", "title": "Pick up food", "done": True},
                    ],
                },
            }
        ],
    }
    operation = parse_plan_bytes(json.dumps(value).encode()).operations[0]
    compiled = compile_create(operation, NOW_MS)

    assert compiled.payload["subtasks"] == {
        "sub-1": {"_id": "sub-1", "title": "Order food", "rank": 1, "done": False},
        "sub-2": {
            "_id": "sub-2",
            "title": "Pick up food",
            "rank": 2,
            "done": True,
            "doneAt": NOW_MS,
        },
    }
