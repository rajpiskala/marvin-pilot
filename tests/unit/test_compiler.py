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
