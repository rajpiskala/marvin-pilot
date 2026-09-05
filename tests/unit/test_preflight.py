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
    revision_snapshot,
    validate_plan_live,
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

    def get_children(self, parent_id: str) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(document)
            for document in self.documents.values()
            if document.get("parentId") == parent_id and not document.get("deletedAt")
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


def same_target_plan():
    value = {
        "schemaVersion": 1,
        "planId": "99999999-9999-4999-8999-999999999999",
        "createdAt": "2026-08-08T14:45:00-07:00",
        "summary": "Rename, reschedule, and complete one task in order.",
        "operations": [
            {
                "operationId": "rename-dishes",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-wash-dishes-id",
                    "title": "Wash the dishes",
                },
                "reason": "Clarify the outcome.",
                "before": {"title": "Wash the dishes"},
                "after": {"title": "Wash the kitchen dishes"},
                "expectedUpdatedAt": 100,
            },
            {
                "operationId": "reschedule-dishes",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-wash-dishes-id",
                    "title": "Wash the kitchen dishes",
                },
                "reason": "Record the final scheduled day.",
                "dependsOnOperations": ["rename-dishes"],
                "before": {"scheduledDate": "2026-08-08"},
                "after": {"scheduledDate": "2026-08-09"},
            },
            {
                "operationId": "complete-dishes",
                "action": "complete",
                "target": {
                    "type": "task",
                    "id": "task-wash-dishes-id",
                    "title": "Wash the kitchen dishes",
                },
                "reason": "Close the fully described task.",
                "dependsOnOperations": ["reschedule-dishes"],
                "completedAt": "2026-08-08T14:30:00-07:00",
            },
        ],
    }
    return parse_plan_bytes(json.dumps(value).encode())


def test_whole_plan_preflight_compiles_without_writes(documents: dict) -> None:
    reader = FakeReader(documents)
    progress: list[tuple[int, int, str]] = []
    result = preflight_plan(
        example_plan(),
        reader,
        now_ms=NOW_MS,
        progress=lambda current, total, operation_id: progress.append(
            (current, total, operation_id)
        ),
    )
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
    assert progress == [
        entry
        for index, operation in enumerate(example_plan().operations, start=1)
        for entry in (
            (index - 1, 4, operation.operationId),
            (index, 4, operation.operationId),
        )
    ]


def test_live_preflight_projects_an_explicit_same_target_chain(documents: dict) -> None:
    reader = FakeReader(documents)

    result = preflight_plan(same_target_plan(), reader, now_ms=NOW_MS)

    assert reader.calls.count("task-wash-dishes-id") == 1
    assert result.operations[-1].compiled.desired_fields["day"] == "2026-08-08"
    rename, reschedule, complete = result.operations
    assert rename.prior_same_target_operation_id is None
    assert reschedule.prior_same_target_operation_id == "rename-dishes"
    assert complete.prior_same_target_operation_id == "reschedule-dishes"
    assert reschedule.live_document["title"] == "Wash the kitchen dishes"
    assert reschedule.live_document["updatedAt"] == NOW_MS
    assert complete.live_document["day"] == "2026-08-09"
    assert complete.compiled.before_fields["done"] == {"present": False}


def test_live_preflight_rejects_stale_completion_day_lock(documents: dict) -> None:
    value = same_target_plan().model_dump(mode="json", by_alias=True, exclude_none=True)
    value["operations"][-1]["completionDay"] = {
        "before": "2026-08-08",
        "after": "2026-08-08",
        "behavior": "preserved",
    }
    plan = parse_plan_bytes(json.dumps(value).encode())

    with pytest.raises(LivePreconditionError, match="completion day is stale"):
        preflight_plan(plan, FakeReader(documents), now_ms=NOW_MS)


def test_live_validation_blocks_later_same_target_steps_after_an_error(documents: dict) -> None:
    documents["task-wash-dishes-id"]["title"] = "Unexpected live title"

    result = validate_plan_live(same_target_plan(), FakeReader(documents), now_ms=NOW_MS)

    assert [diagnostic.check for diagnostic in result.errors] == [
        "target-title",
        "same-target-predecessor",
        "same-target-predecessor",
    ]
    assert result.operations == ()


def test_selected_later_chain_step_validates_against_current_live_state(documents: dict) -> None:
    documents["task-wash-dishes-id"].update(
        {"title": "Wash the kitchen dishes", "updatedAt": NOW_MS}
    )

    result = validate_plan_live(
        same_target_plan(),
        FakeReader(documents),
        now_ms=NOW_MS,
        selected_indices=(2,),
    )

    assert result.valid
    assert result.operations[0].prior_same_target_operation_id is None


def test_later_reference_checks_see_an_earlier_project_rename(documents: dict) -> None:
    documents["project-release"] = {
        "_id": "project-release",
        "_rev": "1-project",
        "db": "Categories",
        "type": "project",
        "title": "Old release name",
        "parentId": "root",
        "updatedAt": 300,
    }
    value = {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "createdAt": "2026-08-08T14:45:00-07:00",
        "summary": "Rename a project before moving a task into it.",
        "operations": [
            {
                "operationId": "rename-release-project",
                "action": "update",
                "target": {
                    "type": "project",
                    "id": "project-release",
                    "title": "Old release name",
                },
                "reason": "Use the durable project name.",
                "before": {"title": "Old release name"},
                "after": {"title": "Release archive"},
                "expectedUpdatedAt": 300,
            },
            {
                "operationId": "move-dinner-task",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-dinner-id",
                    "title": "Eat dinner with Jacob",
                },
                "reason": "File the task under the renamed project.",
                "dependsOnOperations": ["rename-release-project"],
                "before": {"parent": None},
                "after": {"parent": {"id": "project-release", "title": "Release archive"}},
            },
        ],
    }

    result = preflight_plan(
        parse_plan_bytes(json.dumps(value).encode()), FakeReader(documents), now_ms=NOW_MS
    )

    assert result.warnings == ()
    assert result.operations[1].compiled.desired_fields["parentId"] == "project-release"


def test_rename_move_and_project_completion_compose_in_one_plan(documents: dict) -> None:
    documents["project-release"] = {
        "_id": "project-release",
        "_rev": "1-project",
        "db": "Categories",
        "type": "project",
        "title": "Old release name",
        "parentId": "root",
        "updatedAt": 300,
    }
    value = {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff",
        "createdAt": "2026-08-08T14:45:00-07:00",
        "summary": "Rename, populate, and complete one project atomically.",
        "operations": [
            {
                "operationId": "rename-release",
                "action": "update",
                "target": {"type": "project", "id": "project-release", "title": "Old release name"},
                "reason": "Use the final name.",
                "before": {"title": "Old release name"},
                "after": {"title": "Release archive"},
                "expectedUpdatedAt": 300,
            },
            {
                "operationId": "move-dinner",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-dinner-id",
                    "title": "Eat dinner with Jacob",
                },
                "reason": "File the task.",
                "dependsOnOperations": ["rename-release"],
                "before": {"parent": None},
                "after": {"parent": {"id": "project-release", "title": "Release archive"}},
            },
            {
                "operationId": "complete-release",
                "action": "complete",
                "target": {"type": "project", "id": "project-release", "title": "Release archive"},
                "reason": "The archive is now complete.",
                "dependsOnOperations": ["rename-release", "move-dinner"],
                "completedAt": "2026-08-08T14:00:00-07:00",
            },
        ],
    }

    result = preflight_plan(
        parse_plan_bytes(json.dumps(value).encode()), FakeReader(documents), now_ms=NOW_MS
    )

    assert [item.operation.operationId for item in result.operations] == [
        "rename-release",
        "move-dinner",
        "complete-release",
    ]
    assert result.operations[-1].live_document["title"] == "Release archive"


def test_relative_today_order_resolves_next_float_and_orbit_is_allowlisted(
    documents: dict,
) -> None:
    documents["task-wash-dishes-id"]["rank"] = 10.0
    documents["task-wash-dishes-id"]["orbit"] = False
    documents["anchor"] = {
        "_id": "anchor",
        "db": "Tasks",
        "title": "Anchor",
        "day": "2026-08-08",
        "rank": 20.0,
    }
    value = {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-bbbb-4ccc-8ddd-aaaaaaaaaaaa",
        "createdAt": "2026-08-08T14:45:00-07:00",
        "summary": "Orbit and position one task.",
        "operations": [
            {
                "operationId": "orbit-and-order",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-wash-dishes-id",
                    "title": "Wash the dishes",
                },
                "reason": "Put it after its prerequisite.",
                "before": {"orbit": False},
                "after": {"orbit": True},
                "siblingOrder": {"afterId": "anchor"},
            }
        ],
    }
    result = preflight_plan(
        parse_plan_bytes(json.dumps(value).encode()), FakeReader(documents), now_ms=NOW_MS
    )
    compiled = result.operations[0].compiled
    assert compiled.desired_fields["orbit"] is True
    assert compiled.desired_fields["rank"] > 20.0
    assert compiled.before_fields["rank"] == {"present": True, "value": 10.0}


def test_relative_order_requires_anchor_in_same_day(documents: dict) -> None:
    documents["anchor"] = {
        "_id": "anchor",
        "db": "Tasks",
        "title": "Anchor",
        "day": "2026-08-09",
        "rank": 20.0,
    }
    value = {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-bbbb-4ccc-8ddd-bbbbbbbbbbbb",
        "createdAt": "2026-08-08T14:45:00-07:00",
        "summary": "Reject a cross-day relative order.",
        "operations": [
            {
                "operationId": "bad-order",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "task-wash-dishes-id",
                    "title": "Wash the dishes",
                },
                "reason": "Exercise the boundary.",
                "before": {},
                "after": {},
                "siblingOrder": {"beforeId": "anchor"},
            }
        ],
    }
    with pytest.raises(LivePreconditionError, match="not on the same day"):
        preflight_plan(
            parse_plan_bytes(json.dumps(value).encode()), FakeReader(documents), now_ms=NOW_MS
        )


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


def test_parent_reference_must_exist_but_a_stale_hint_warns(documents: dict) -> None:
    del documents["people-category-id"]
    with pytest.raises(LivePreconditionError, match="missing parent"):
        preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)
    documents["people-category-id"] = {"db": "Categories", "title": "Friends"}
    result = preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)
    assert [(warning.check, warning.expected, warning.found) for warning in result.warnings] == [
        ("parent-title-hint", "People", "Friends")
    ]


def test_unassigned_parent_hint_warns_when_it_does_not_say_inbox(documents: dict) -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    value["operations"][1]["before"]["parent"]["title"] = "Not Inbox"
    plan = parse_plan_bytes(json.dumps(value).encode())
    result = preflight_plan(plan, FakeReader(documents), now_ms=NOW_MS)
    warning = result.warnings[0]
    assert warning.check == "parent-title-hint"
    assert warning.expected == "Not Inbox"
    assert warning.found == "Inbox"


def test_live_validation_collects_all_errors_with_one_shared_read_cache(documents: dict) -> None:
    documents["task-wash-dishes-id"]["title"] = "Changed wash title"
    documents["task-dinner-id"]["title"] = "Changed dinner title"
    documents["40d06376-9125-4e9e-a6bd-631cb0e6dc55"] = {
        "_id": "40d06376-9125-4e9e-a6bd-631cb0e6dc55",
        "db": "Tasks",
        "title": "ID collision",
    }
    documents["duplicate-task-id"]["updatedAt"] = 999
    reader = FakeReader(documents)

    result = validate_plan_live(example_plan(), reader, now_ms=NOW_MS)

    assert not result.valid
    assert [item.operation_index for item in result.errors] == [1, 2, 3, 4]
    assert [item.check for item in result.errors] == [
        "target-title",
        "target-title",
        "create-id-available",
        "updated-at",
    ]
    assert result.errors[0].expected == "Wash the dishes"
    assert result.errors[0].found == "Changed wash title"
    for item_id in (
        "task-wash-dishes-id",
        "task-dinner-id",
        "40d06376-9125-4e9e-a6bd-631cb0e6dc55",
        "duplicate-task-id",
    ):
        assert reader.calls.count(item_id) == 1

    with pytest.raises(LivePreconditionError, match="found 4 errors"):
        preflight_plan(example_plan(), FakeReader(documents), now_ms=NOW_MS)


def test_live_validation_supports_selection_and_fail_fast(documents: dict) -> None:
    documents["task-wash-dishes-id"]["title"] = "Changed wash title"
    documents["duplicate-task-id"]["updatedAt"] = 999
    reader = FakeReader(documents)

    selected = validate_plan_live(example_plan(), reader, now_ms=NOW_MS, selected_indices=(3, 4))
    assert [item.operation_index for item in selected.errors] == [4]
    assert "task-wash-dishes-id" not in reader.calls
    assert "task-dinner-id" not in reader.calls

    stopped = validate_plan_live(
        example_plan(), FakeReader(documents), now_ms=NOW_MS, fail_fast=True
    )
    assert [item.operation_index for item in stopped.errors] == [1]
    assert stopped.operations == ()


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

    documents["label-math"]["title"] = "Arithmetic"
    result = preflight_plan(plan, FakeReader(documents), now_ms=NOW_MS)
    assert [(warning.check, warning.expected, warning.found) for warning in result.warnings] == [
        ("label-title-hint", "Math", "Arithmetic")
    ]


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


def test_same_target_recheck_uses_the_actual_prior_write_revision(documents: dict) -> None:
    checked = preflight_plan(same_target_plan(), FakeReader(documents), now_ms=NOW_MS)
    later = checked.operations[1]
    post_prior = copy.deepcopy(later.live_document)
    post_prior["_rev"] = "2-after-own-write"
    expected_revision = revision_snapshot(post_prior)

    assert (
        recheck_operation(
            later,
            FakeReader({"task-wash-dishes-id": post_prior}),
            strict_concurrency=True,
            expected_revision=expected_revision,
        )
        is not None
    )

    post_prior["_rev"] = "3-external-edit"
    with pytest.raises(LivePreconditionError, match="_rev changed"):
        recheck_operation(
            later,
            FakeReader({"task-wash-dishes-id": post_prior}),
            strict_concurrency=True,
            expected_revision=expected_revision,
        )


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


def _subtask_conversion_plan(*, accept_loss: list[str] | None = None):
    source_task = {"id": "loose-order", "title": "Order food"}
    if accept_loss is not None:
        source_task["acceptLoss"] = accept_loss
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
                            "sourceTask": source_task,
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
    documents = _subtask_documents()
    documents["loose-order"].update(
        {
            "rank": 500,
            "masterRank": 900,
            "firstScheduled": "2026-07-01",
            "workedOnAt": 1_786_200_000_000,
        }
    )
    result = preflight_plan(_subtask_conversion_plan(), FakeReader(documents), now_ms=NOW_MS)
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
        (
            lambda docs: docs["loose-order"].update({"note": "Keep this"}),
            "acceptLoss.*note",
        ),
        (
            lambda docs: docs["loose-order"].update({"isStarred": 1}),
            "acceptLoss.*starPriority",
        ),
        (lambda docs: docs["loose-order"].update({"isPinned": True}), "coupled behavior"),
        (lambda docs: docs["loose-order"].update({"subtasks": {"one": {}}}), "existing subtasks"),
        (lambda docs: docs["loose-order"].update({"dependsOn": {"task-a": True}}), "dependencies"),
        (lambda docs: docs["loose-order"].update({"duration": 600_000}), "tracked time"),
    ],
)
def test_subtask_conversion_rejects_lossy_or_stale_sources(mutation, message: str) -> None:
    documents = _subtask_documents()
    mutation(documents)
    with pytest.raises(LivePreconditionError, match=message):
        preflight_plan(_subtask_conversion_plan(), FakeReader(documents), now_ms=NOW_MS)


def test_subtask_conversion_requires_exact_loss_acknowledgments() -> None:
    documents = _subtask_documents()
    documents["loose-order"]["timeEstimate"] = 600_000

    result = preflight_plan(
        _subtask_conversion_plan(accept_loss=["estimatedTimeDuration"]),
        FakeReader(documents),
        now_ms=NOW_MS,
    )

    assert [(warning.check, warning.expected, warning.found) for warning in result.warnings] == [
        ("source-task-accepted-loss", [], ["estimatedTimeDuration"])
    ]
    assert "explicitly accepts sourceTask" in result.warnings[0].message

    with pytest.raises(LivePreconditionError, match=r"not present.*note"):
        preflight_plan(
            _subtask_conversion_plan(accept_loss=["estimatedTimeDuration", "note"]),
            FakeReader(documents),
            now_ms=NOW_MS,
        )
