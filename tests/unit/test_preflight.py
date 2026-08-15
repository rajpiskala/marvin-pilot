from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from marvin_pilot.errors import LivePreconditionError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.preflight import (
    coupled_task_reasons,
    desired_fields_match,
    preflight_plan,
    recheck_operation,
)

NOW_MS = 1_786_233_600_123


class FakeReader:
    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = copy.deepcopy(documents)
        self.calls: list[str] = []
        self.label_calls = 0

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        self.calls.append(item_id)
        document = self.documents.get(item_id)
        return copy.deepcopy(document) if document is not None else None

    def get_labels(self) -> list[dict[str, Any]]:
        self.label_calls += 1
        return [
            copy.deepcopy(document | {"_id": item_id})
            for item_id, document in self.documents.items()
            if document.get("db") == "Labels"
        ]


@pytest.fixture
def documents() -> dict[str, dict[str, Any]]:
    return {
        "task-wash-dishes-id": {
            "_id": "task-wash-dishes-id",
            "_rev": "1-wash",
            "db": "Tasks",
            "title": "Wash the dishes",
            "day": "2026-08-08",
            "firstScheduled": "2026-08-01",
            "updatedAt": 100,
        },
        "task-dinner-id": {
            "_id": "task-dinner-id",
            "_rev": "1-dinner",
            "db": "Tasks",
            "title": "Eat dinner with Jacob",
            "parentId": "unassigned",
            "updatedAt": 200,
        },
        "duplicate-task-id": {
            "_id": "duplicate-task-id",
            "_rev": "1-duplicate",
            "db": "Tasks",
            "title": "Study chapter 3",
            "updatedAt": 1_786_221_000_123,
        },
        "people-category-id": {
            "_id": "people-category-id",
            "db": "Categories",
            "title": "People",
        },
    }


def example_plan():
    return parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())


def test_whole_plan_preflight_compiles_without_writes(documents: dict) -> None:
    reader = FakeReader(documents)
    result = preflight_plan(example_plan(), reader, now_ms=NOW_MS)
    assert len(result.operations) == 4
    assert [item.compiled.action for item in result.operations] == [
        "update",
        "update",
        "create",
        "trash",
    ]
    assert result.checked_at_ms == NOW_MS
    assert result.operations[0].live_revision["_rev"] == {
        "present": True,
        "value": "1-wash",
    }
    assert reader.calls.count("people-category-id") == 1
    assert reader.calls.count("40d06376-9125-4e9e-a6bd-631cb0e6dc55") == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda docs: docs["task-wash-dishes-id"].update({"title": "Changed"}), "title is stale"),
        (lambda docs: docs["task-wash-dishes-id"].update({"day": "2026-08-07"}), "stale fields"),
        (lambda docs: docs["task-wash-dishes-id"].update({"db": "Categories"}), "non-Task"),
        (lambda docs: docs.pop("task-wash-dishes-id"), "does not exist"),
        (
            lambda docs: docs.update(
                {
                    "40d06376-9125-4e9e-a6bd-631cb0e6dc55": {
                        "db": "Tasks",
                        "title": "Collision",
                    }
                }
            ),
            "target ID already exists",
        ),
        (
            lambda docs: docs["duplicate-task-id"].update({"updatedAt": 999}),
            "updatedAt is stale",
        ),
        (
            lambda docs: docs["duplicate-task-id"].update({"deletedAt": 123}),
            "already in Trash",
        ),
        (
            lambda docs: docs["task-wash-dishes-id"].update({"deletedAt": 123}),
            "currently in Trash",
        ),
    ],
)
def test_preflight_rejects_stale_or_wrong_targets(documents: dict, mutation, message: str) -> None:
    mutation(documents)
    with pytest.raises(LivePreconditionError, match=message):
        preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"recurring": {"type": "daily"}}, "recurrence"),
        ({"echo": True}, "recurrence"),
        ({"isPinned": True}, "pinned"),
        ({"pinId": "pin-a"}, "pinned"),
        ({"isReward": True}, "reward"),
        ({"isTracking": True}, "tracking"),
        ({"reminderTime": 123}, "reminder"),
        ({"calId": "calendar"}, "calendar"),
    ],
)
def test_coupled_tasks_are_blocked(documents: dict, fields: dict, reason: str) -> None:
    documents["task-wash-dishes-id"].update(fields)
    assert any(reason in item for item in coupled_task_reasons(documents["task-wash-dishes-id"]))
    with pytest.raises(LivePreconditionError, match="coupled behavior"):
        preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)


def test_parent_reference_must_exist_and_match_hint(documents: dict) -> None:
    del documents["people-category-id"]
    with pytest.raises(LivePreconditionError, match="missing parent"):
        preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)
    documents["people-category-id"] = {"db": "Categories", "title": "Friends"}
    with pytest.raises(LivePreconditionError, match="parent title is stale"):
        preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)


def test_unassigned_parent_hint_must_say_inbox(documents: dict) -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    value["operations"][1]["before"]["parent"]["title"] = "Not Inbox"
    plan = parse_plan_bytes(json.dumps(value).encode())
    with pytest.raises(LivePreconditionError, match="not 'Inbox'"):
        preflight_plan(plan, FakeReader(documents), now_ms=NOW_MS)


def test_label_and_dependency_references_are_verified(documents: dict) -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    operation = value["operations"][0]
    operation["before"] = {"labels": None, "dependencies": None}
    operation["after"] = {
        "labels": [{"id": "label-math", "title": "Math"}],
        "dependencies": ["task-dependency"],
    }
    plan = parse_plan_bytes(json.dumps(value).encode())
    with pytest.raises(LivePreconditionError, match="missing label"):
        preflight_plan(plan, FakeReader(documents), now_ms=NOW_MS)
    documents["label-math"] = {"db": "Labels", "title": "Math"}
    with pytest.raises(LivePreconditionError, match="missing dependency"):
        preflight_plan(plan, FakeReader(documents), now_ms=NOW_MS)
    documents["task-dependency"] = {"db": "Tasks", "title": "Dependency"}
    reader = FakeReader(documents)
    preflight_plan(plan, reader, now_ms=NOW_MS)
    assert reader.label_calls == 1


def test_recheck_detects_revision_change_and_disappearance(documents: dict) -> None:
    result = preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)
    wash = result.operations[0]
    documents["task-wash-dishes-id"]["_rev"] = "2-wash"
    with pytest.raises(LivePreconditionError, match="_rev changed"):
        recheck_operation(wash, FakeReader(documents), strict_concurrency=True)
    del documents["task-wash-dishes-id"]
    with pytest.raises(LivePreconditionError, match="disappeared"):
        recheck_operation(wash, FakeReader(documents), strict_concurrency=True)


def test_recheck_can_use_relevant_fields_when_strict_mode_is_off(documents: dict) -> None:
    result = preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)
    wash = result.operations[0]
    documents["task-wash-dishes-id"]["_rev"] = "2-wash"
    assert recheck_operation(wash, FakeReader(documents), strict_concurrency=False) is not None


def test_recheck_create_detects_new_collision(documents: dict) -> None:
    result = preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)
    create = result.operations[2]
    documents[create.operation.target.id] = {"db": "Tasks", "title": "Appeared"}
    with pytest.raises(LivePreconditionError, match="appeared after preflight"):
        recheck_operation(create, FakeReader(documents), strict_concurrency=True)


def test_desired_field_verification_is_exact() -> None:
    assert desired_fields_match({"title": "New", "day": "2026-08-09"}, {"title": "New"})
    assert not desired_fields_match({"title": "Other"}, {"title": "New"})
    assert not desired_fields_match({}, {"title": None})
    assert not desired_fields_match(None, {})


def _subtask_conversion_plan():
    value = {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "createdAt": "2026-08-14T08:00:00-07:00",
        "summary": "Consolidate a loose dinner task.",
        "operations": [
            {
                "operationId": "build-dinner-checklist",
                "action": "update",
                "target": {"type": "task", "id": "parent-task", "title": "Handle dinner"},
                "reason": "Put the workflow in one ordered checklist.",
                "before": {"subtasks": []},
                "after": {
                    "subtasks": [
                        {
                            "id": "sub-order",
                            "title": "Order food",
                            "sourceTask": {"id": "loose-order", "title": "Order food"},
                        }
                    ]
                },
            },
            {
                "operationId": "trash-loose-order",
                "action": "trash",
                "target": {"type": "task", "id": "loose-order", "title": "Order food"},
                "reason": "The new subtask replaces this loose task.",
                "dependsOnOperations": ["build-dinner-checklist"],
            },
        ],
    }
    return parse_plan_bytes(json.dumps(value).encode())


def _subtask_documents() -> dict[str, dict[str, Any]]:
    return {
        "parent-task": {
            "_id": "parent-task",
            "_rev": "1-parent",
            "db": "Tasks",
            "title": "Handle dinner",
            "subtasks": {},
            "updatedAt": 100,
        },
        "loose-order": {
            "_id": "loose-order",
            "_rev": "1-source",
            "db": "Tasks",
            "title": "Order food",
            "done": False,
            "day": "unassigned",
            "parentId": "unassigned",
            "updatedAt": 200,
        },
    }


def test_subtask_conversion_preflight_preserves_source_and_compiles_atomically() -> None:
    result = preflight_plan(
        _subtask_conversion_plan(), FakeReader(_subtask_documents()), now_ms=NOW_MS
    )
    update, trash = result.operations
    subtasks = next(
        setter["val"]
        for setter in update.compiled.payload["setters"]
        if setter["key"] == "subtasks"
    )
    assert subtasks == {
        "sub-order": {"_id": "sub-order", "title": "Order food", "rank": 1, "done": False}
    }
    assert trash.operation.dependsOnOperations == ["build-dinner-checklist"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda docs: docs.pop("loose-order"), "missing or non-Task"),
        (lambda docs: docs["loose-order"].update({"title": "Order takeout"}), "title is stale"),
        (
            lambda docs: docs["loose-order"].update({"done": True}),
            "cannot convert completed sourceTask",
        ),
        (lambda docs: docs["loose-order"].update({"note": "Keep this"}), "not represented.*note"),
        (
            lambda docs: docs["loose-order"].update({"isStarred": 1}),
            "not represented.*isStarred",
        ),
        (lambda docs: docs["loose-order"].update({"isPinned": True}), "coupled behavior"),
    ],
)
def test_subtask_conversion_rejects_lossy_or_stale_sources(mutation, message: str) -> None:
    documents = _subtask_documents()
    mutation(documents)
    with pytest.raises(LivePreconditionError, match=message):
        preflight_plan(_subtask_conversion_plan(), FakeReader(documents), now_ms=NOW_MS)
