from __future__ import annotations

import copy
import json

import pytest

from marvin_pilot.errors import LivePreconditionError, PlanSyntaxError
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.preflight import preflight_plan


class Reader:
    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = copy.deepcopy(documents)

    def get_doc(self, item_id: str):
        return copy.deepcopy(self.documents.get(item_id))

    def get_labels(self):
        return []

    def get_children(self, parent_id: str):
        return [
            copy.deepcopy(item)
            for item in self.documents.values()
            if item.get("parentId") == parent_id
        ]


def _base(operation: dict) -> dict:
    return {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "createdAt": "2026-09-04T12:00:00-07:00",
        "summary": "Exercise category lifecycle.",
        "operations": [operation],
    }


def test_category_create_compiles_native_category_document() -> None:
    plan = parse_plan_bytes(
        json.dumps(
            _base(
                {
                    "operationId": "create-category",
                    "action": "create",
                    "target": {
                        "type": "category",
                        "id": "11111111-1111-4111-8111-111111111111",
                    },
                    "after": {"title": "New category"},
                    "reason": "Add a root folder.",
                }
            )
        ).encode()
    )
    result = preflight_plan(plan, Reader({}), now_ms=1_788_550_400_000)
    assert result.operations[0].compiled.payload["db"] == "Categories"
    assert result.operations[0].compiled.payload["type"] == "category"


def test_category_completion_is_rejected() -> None:
    with pytest.raises(PlanSyntaxError, match="categories cannot be completed"):
        parse_plan_bytes(
            json.dumps(
                _base(
                    {
                        "operationId": "complete-category",
                        "action": "complete",
                        "target": {"type": "category", "id": "cat", "title": "Category"},
                        "completedAt": "2026-09-04T11:00:00-07:00",
                        "reason": "Invalid by design.",
                    }
                )
            ).encode()
        )


def test_category_trash_requires_all_direct_children_moved_first() -> None:
    documents = {
        "cat": {
            "_id": "cat",
            "db": "Categories",
            "type": "category",
            "title": "Category",
        },
        "child": {
            "_id": "child",
            "db": "Tasks",
            "title": "Child task",
            "parentId": "cat",
        },
    }
    plan = parse_plan_bytes(
        json.dumps(
            _base(
                {
                    "operationId": "trash-category",
                    "action": "trash",
                    "target": {"type": "category", "id": "cat", "title": "Category"},
                    "reason": "Remove an empty category.",
                }
            )
        ).encode()
    )
    with pytest.raises(LivePreconditionError, match="non-empty category"):
        preflight_plan(plan, Reader(documents), now_ms=1_788_550_400_000)


def test_category_move_then_trash_projects_child_out_of_container() -> None:
    documents = {
        "cat": {
            "_id": "cat",
            "db": "Categories",
            "type": "category",
            "title": "Old category",
            "parentId": "unassigned",
            "rank": 20,
        },
        "destination": {
            "_id": "destination",
            "db": "Categories",
            "type": "category",
            "title": "Destination",
            "parentId": "unassigned",
            "rank": 10,
        },
        "child": {
            "_id": "child",
            "db": "Categories",
            "type": "project",
            "title": "Child project",
            "parentId": "cat",
        },
    }
    value = _base(
        {
            "operationId": "unused",
            "action": "trash",
            "target": {"type": "task", "id": "unused", "title": "unused"},
            "reason": "Replaced below.",
        }
    )
    value["operations"] = [
        {
            "operationId": "rename-and-order-category",
            "action": "update",
            "target": {"type": "category", "id": "cat", "title": "Old category"},
            "before": {"title": "Old category"},
            "after": {"title": "Archived category"},
            "siblingOrder": {"afterId": "destination"},
            "reason": "Give the category its final title and relative position.",
        },
        {
            "operationId": "move-child",
            "action": "update",
            "target": {"type": "project", "id": "child", "title": "Child project"},
            "before": {"parent": {"id": "cat", "title": "Archived category"}},
            "after": {"parent": {"id": "destination", "title": "Destination"}},
            "dependsOnOperations": ["rename-and-order-category"],
            "reason": "Empty the old category.",
        },
        {
            "operationId": "trash-category",
            "action": "trash",
            "target": {"type": "category", "id": "cat", "title": "Archived category"},
            "dependsOnOperations": ["rename-and-order-category", "move-child"],
            "reason": "Delete the now-empty category.",
        },
    ]
    plan = parse_plan_bytes(json.dumps(value).encode())
    result = preflight_plan(plan, Reader(documents), now_ms=1_788_550_400_000)
    first = result.operations[0].compiled
    assert first.desired_fields["title"] == "Archived category"
    assert first.desired_fields["rank"] > documents["destination"]["rank"]
    assert result.operations[-1].compiled.endpoint == "doc/delete"
