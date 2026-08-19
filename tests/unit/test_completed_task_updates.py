from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from marvin_pilot.errors import LivePreconditionError
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.preflight import preflight_plan

NOW_MS = 1_787_078_400_123
DONE_AT_MS = 1_784_856_600_000
DONE_AT = "2026-07-23T18:30:00-07:00"


class Reader:
    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = copy.deepcopy(documents)

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        value = self.documents.get(item_id)
        return copy.deepcopy(value) if value is not None else None

    def get_labels(self) -> list[dict[str, Any]]:
        return []


def completed_reparent_plan() -> dict[str, Any]:
    old_project = {"id": "project-old", "type": "project", "title": "Project Alpha"}
    new_project = {"id": "project-new", "type": "project", "title": "Project Beta"}
    return {
        "schemaVersion": 1,
        "planId": "1f7c28a6-6bbd-4629-93c3-73517834a169",
        "createdAt": "2026-08-18T12:00:00-07:00",
        "summary": "Reorganize one completed task without reopening it.",
        "operations": [
            {
                "operationId": "move-completed-task",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-completed",
                    "title": "Prepare release notes",
                },
                "reason": "Place the historical work under its durable project.",
                "expectedUpdatedAt": 100,
                "before": {"parent": {"id": "project-old", "title": "Project Alpha"}},
                "after": {"parent": {"id": "project-new", "title": "Project Beta"}},
                "display": {
                    "beforePath": [old_project],
                    "afterPath": [new_project],
                    "existingCompletedAt": DONE_AT,
                },
            }
        ],
    }


def completed_documents() -> dict[str, dict[str, Any]]:
    return {
        "task-completed": {
            "_id": "task-completed",
            "_rev": "1-completed",
            "db": "Tasks",
            "title": "Prepare release notes",
            "parentId": "project-old",
            "done": True,
            "doneAt": DONE_AT_MS,
            "updatedAt": 100,
        },
        "project-old": {
            "_id": "project-old",
            "db": "Categories",
            "type": "project",
            "title": "Project Alpha",
            "parentId": "unassigned",
        },
        "project-new": {
            "_id": "project-new",
            "db": "Categories",
            "type": "project",
            "title": "Project Beta",
            "parentId": "unassigned",
        },
    }


def test_completed_task_reparent_compiles_only_parent_fields() -> None:
    plan = parse_plan_bytes(json.dumps(completed_reparent_plan()).encode())
    result = preflight_plan(plan, Reader(completed_documents()), now_ms=NOW_MS)
    compiled = result.operations[0].compiled

    assert compiled.desired_fields == {"parentId": "project-new"}
    assert compiled.before_fields == {"parentId": {"present": True, "value": "project-old"}}
    assert compiled.payload["setters"] == [
        {"key": "parentId", "val": "project-new"},
        {"key": "fieldUpdates.parentId", "val": NOW_MS},
        {"key": "updatedAt", "val": NOW_MS},
    ]
    assert not {"done", "doneAt"} & {setter["key"] for setter in compiled.payload["setters"]}


def test_completed_task_update_requires_exact_review_metadata() -> None:
    value = completed_reparent_plan()
    value["operations"][0]["display"].pop("existingCompletedAt")
    plan = parse_plan_bytes(json.dumps(value).encode())
    with pytest.raises(LivePreconditionError, match="targets a completed task"):
        preflight_plan(plan, Reader(completed_documents()), now_ms=NOW_MS)

    value = completed_reparent_plan()
    value["operations"][0]["display"]["existingCompletedAt"] = "2026-07-24T18:30:00-07:00"
    plan = parse_plan_bytes(json.dumps(value).encode())
    with pytest.raises(LivePreconditionError, match="completion timestamp is stale"):
        preflight_plan(plan, Reader(completed_documents()), now_ms=NOW_MS)


def test_open_task_rejects_false_existing_completion_claim() -> None:
    documents = completed_documents()
    documents["task-completed"].update({"done": False})
    documents["task-completed"].pop("doneAt")
    plan = parse_plan_bytes(json.dumps(completed_reparent_plan()).encode())
    with pytest.raises(LivePreconditionError, match="live task is open"):
        preflight_plan(plan, Reader(documents), now_ms=NOW_MS)


def test_completed_task_requires_a_live_completion_timestamp() -> None:
    documents = completed_documents()
    documents["task-completed"].pop("doneAt")
    plan = parse_plan_bytes(json.dumps(completed_reparent_plan()).encode())
    with pytest.raises(LivePreconditionError, match="no exact doneAt timestamp"):
        preflight_plan(plan, Reader(documents), now_ms=NOW_MS)
