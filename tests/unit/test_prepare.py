from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from marvin_pilot.errors import LivePreconditionError, PlanSemanticError, PlanSyntaxError
from marvin_pilot.prepare import SnapshotReader, parse_draft_bytes, prepare_draft, rebase_plan_live

NOW_MS = 1_788_550_400_000


class AccountReader:
    def check_connection(self):
        return SimpleNamespace(account_user_id="123456", account_email="pilot@example.com")


def draft_value() -> dict:
    return {
        "draftVersion": 1,
        "expectedAccount": {"userId": "123456", "email": "pilot@example.com"},
        "summary": "Move one task and create its follow-up.",
        "operations": [
            {
                "operationId": "move-task",
                "action": "update",
                "target": {"type": "task", "id": "task-1"},
                "after": {"parent": {"id": "project-2"}},
                "reason": "Put the task with its actual outcome.",
            },
            {
                "operationId": "create-follow-up",
                "action": "create",
                "target": {"type": "task"},
                "after": {"title": "Confirm the result", "parent": {"id": "project-2"}},
                "reason": "Make the verification explicit.",
            },
        ],
    }


def documents() -> list[dict]:
    return [
        {
            "_id": "work",
            "db": "Categories",
            "type": "category",
            "title": "Work",
            "parentId": "root",
        },
        {
            "_id": "project-1",
            "db": "Categories",
            "type": "project",
            "title": "Old project",
            "parentId": "work",
        },
        {
            "_id": "project-2",
            "db": "Categories",
            "type": "project",
            "title": "New project",
            "parentId": "work",
        },
        {
            "_id": "task-1",
            "db": "Tasks",
            "title": "Do the work",
            "parentId": "project-1",
            "updatedAt": 42,
        },
    ]


def test_prepare_fills_identity_before_lock_uuid_and_paths() -> None:
    draft = parse_draft_bytes(json.dumps(draft_value()).encode())
    plan = prepare_draft(draft, SnapshotReader(documents()), now_ms=NOW_MS)

    move, create = plan.operations
    assert plan.expectedAccount.userId == "123456"
    assert move.target.title == "Do the work"
    assert move.expectedUpdatedAt == 42
    assert move.before.parent.id == "project-1"
    assert [node.title for node in move.display.beforePath] == ["Work", "Old project"]
    assert [node.title for node in move.display.afterPath] == ["Work", "New project"]
    assert create.target.id
    assert [node.title for node in create.display.afterPath] == ["Work", "New project"]


def test_prepare_is_closed_and_never_infers_missing_desired_fields() -> None:
    value = draft_value()
    value["operations"][0]["maybeDate"] = "tomorrow"
    with pytest.raises(PlanSyntaxError, match="Extra inputs are not permitted"):
        parse_draft_bytes(json.dumps(value).encode())


def test_rebase_refreshes_only_lock_metadata_when_before_still_matches() -> None:
    plan = prepare_draft(
        parse_draft_bytes(json.dumps(draft_value()).encode()),
        SnapshotReader(documents()),
        now_ms=NOW_MS,
    )
    updated = documents()
    updated[-1]["updatedAt"] = 99
    rebased = rebase_plan_live(
        plan, SnapshotReader(updated), account_reader=AccountReader(), now_ms=NOW_MS + 1_000
    )
    assert rebased.planId != plan.planId
    assert rebased.operations[0].expectedUpdatedAt == 99
    assert rebased.operations[0].before == plan.operations[0].before
    assert rebased.operations[0].after == plan.operations[0].after


def test_rebase_stops_when_reviewed_before_state_drifted() -> None:
    plan = prepare_draft(
        parse_draft_bytes(json.dumps(draft_value()).encode()),
        SnapshotReader(documents()),
        now_ms=NOW_MS,
    )
    updated = documents()
    updated[-1]["parentId"] = "work"
    with pytest.raises(LivePreconditionError, match="semantic review is required"):
        rebase_plan_live(
            plan, SnapshotReader(updated), account_reader=AccountReader(), now_ms=NOW_MS + 1_000
        )


def test_prepare_preserves_relative_order_and_reconstructs_duration_and_orbit() -> None:
    value = draft_value()
    value["operations"] = [
        {
            "operationId": "update-task",
            "action": "update",
            "target": {"type": "task", "id": "task-1"},
            "after": {"estimatedTimeDuration": "30m", "orbit": True},
            "siblingOrder": {"afterId": "task-2"},
            "reason": "Update exact supported values and its relative order.",
        }
    ]
    source = documents()
    source[-1]["timeEstimate"] = 15 * 60_000
    source[-1]["orbit"] = False
    plan = prepare_draft(
        parse_draft_bytes(json.dumps(value).encode()), SnapshotReader(source), now_ms=NOW_MS
    )
    operation = plan.operations[0]
    assert operation.before.estimatedTimeDuration == "15m"
    assert operation.before.orbit is False
    assert operation.siblingOrder.afterId == "task-2"


def test_prepare_resolves_one_normalized_backup_title_but_rejects_ambiguity() -> None:
    value = draft_value()
    value["operations"] = [
        {
            "operationId": "rename-task",
            "action": "update",
            "target": {"type": "task", "title": "do the work"},
            "after": {"title": "Do the focused work"},
            "reason": "Exercise conservative title resolution.",
        }
    ]
    draft = parse_draft_bytes(json.dumps(value).encode())
    plan = prepare_draft(draft, SnapshotReader(documents()), now_ms=NOW_MS)
    assert plan.operations[0].target.id == "task-1"

    duplicated = [
        *documents(),
        {"_id": "task-2", "db": "Tasks", "title": "Do the work", "parentId": "project-2"},
    ]
    with pytest.raises(PlanSemanticError, match="ambiguous"):
        prepare_draft(draft, SnapshotReader(duplicated), now_ms=NOW_MS)


@pytest.mark.parametrize(
    ("before_day", "behavior"),
    [
        ("unassigned", "assigned"),
        ("2026-09-04", "preserved"),
        ("2026-09-01", "replaced"),
        ("2026-09-07", "replaced"),
    ],
)
def test_prepare_locks_native_completion_day_transition(
    before_day: str, behavior: str
) -> None:
    value = draft_value()
    value["operations"] = [
        {
            "operationId": "complete-task",
            "action": "complete",
            "target": {"type": "task", "id": "task-1"},
            "completedAt": "2026-09-04T10:55:00-07:00",
            "reason": "Record completion in the correct Marvin history bucket.",
        }
    ]
    source = documents()
    source[-1]["day"] = before_day

    plan = prepare_draft(
        parse_draft_bytes(json.dumps(value).encode()),
        SnapshotReader(source),
        now_ms=NOW_MS,
    )

    transition = plan.operations[0].completionDay
    assert transition is not None
    assert transition.before == (None if before_day == "unassigned" else before_day)
    assert transition.after == "2026-09-04"
    assert transition.behavior == behavior
