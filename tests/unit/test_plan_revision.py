from __future__ import annotations

import copy
import json

import pytest

from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import canonical_plan_bytes, parse_plan_bytes, plan_digest
from marvin_pilot.plan_revision import revise_plan


def test_revision_preserves_source_nulls_locks_and_identity_targets() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    operation = value["operations"][1]
    operation.update(expectedUpdatedAt=42)
    operation["before"]["note"] = "Old note"
    operation["after"]["note"] = None
    source = parse_plan_bytes(json.dumps(value).encode())
    before = canonical_plan_bytes(source)
    revised = revise_plan(
        source, [{"operationId": operation["operationId"], "title": "New proposed title"}]
    )
    assert canonical_plan_bytes(source) == before
    assert revised.planId != source.planId
    assert plan_digest(revised) != plan_digest(source)
    assert revised.operations[1].target == source.operations[1].target
    assert revised.operations[1].before == source.operations[1].before
    assert revised.operations[1].expectedUpdatedAt == 42
    assert revised.operations[1].after.note is None
    assert "note" in revised.operations[1].after.model_fields_set
    assert revised.operations[1].after.title == "New proposed title"


def test_exclusion_keeps_order_and_other_operations_exact() -> None:
    source = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    revised = revise_plan(
        source, [{"operationId": source.operations[-1].operationId, "exclude": True}]
    )
    assert [op.model_dump(exclude_unset=True) for op in revised.operations] == [
        op.model_dump(exclude_unset=True) for op in source.operations[:-1]
    ]


@pytest.mark.parametrize(
    "edits",
    [
        [],
        [{"operationId": "unknown", "exclude": True}],
        [{"operationId": "create-email-follow-up", "title": ""}],
        [{"operationId": "create-email-follow-up", "action": "trash"}],
        [{"operationId": "create-email-follow-up", "exclude": True, "title": "Ignored"}],
        [{"operationId": "create-email-follow-up", "completedAt": "2026-08-01T12:00:00Z"}],
        [{"operationId": "reschedule-wash-dishes", "title": "Not a title update"}],
        [{"operationId": "create-email-follow-up"}],
        [{"operationId": "create-email-follow-up", "exclude": True}] * 2,
    ],
)
def test_invalid_or_broad_revisions_are_rejected(edits) -> None:
    source = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    with pytest.raises((PlanSemanticError, PlanSyntaxError)):
        revise_plan(source, edits)


def test_excluding_dependencies_requires_explicit_dependent_exclusion() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    value["operations"][1]["dependsOnOperations"] = [value["operations"][0]["operationId"]]
    source = parse_plan_bytes(json.dumps(value).encode())
    edits = [{"operationId": value["operations"][0]["operationId"], "exclude": True}]
    with pytest.raises(PlanSemanticError, match="dependency"):
        revise_plan(source, edits)
    edits.append({"operationId": value["operations"][1]["operationId"], "exclude": True})
    assert len(revise_plan(source, edits).operations) == 2


def test_completion_edit_updates_transition_but_preserves_before_day_and_lock() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    value["operations"] = [
        {
            "action": "complete",
            "operationId": "finish",
            "target": {"type": "task", "id": "existing", "title": "Existing task"},
            "expectedUpdatedAt": 42,
            "reason": "Finished the work",
            "completedAt": "2026-08-07T18:30:00-07:00",
            "completionDay": {
                "before": "2026-08-07",
                "after": "2026-08-07",
                "behavior": "preserved",
            },
        }
    ]
    source = parse_plan_bytes(json.dumps(value).encode())
    edited = revise_plan(
        source, [{"operationId": "finish", "completedAt": "2026-08-06T18:30:00-07:00"}]
    ).operations[0]
    assert edited.expectedUpdatedAt == 42
    assert edited.completionDay.before == "2026-08-07"
    assert edited.completionDay.after == "2026-08-06"
    assert edited.completionDay.behavior == "replaced"
    with pytest.raises(PlanSyntaxError):
        revise_plan(source, [{"operationId": "finish", "completedAt": "2026-08-06T18:30:00"}])


def test_title_change_projects_through_same_target_chain() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    first = value["operations"][1]
    first["after"] = {"title": "Old proposed title"}
    first["before"] = {"title": first["target"]["title"]}
    second = {
        "operationId": "finish",
        "action": "complete",
        "target": {**first["target"], "title": "Old proposed title"},
        "reason": "Finished",
        "dependsOnOperations": [first["operationId"]],
        "completedAt": "2026-08-07T18:00:00-07:00",
    }
    value["operations"] = [first, second]
    source = parse_plan_bytes(json.dumps(value).encode())
    revised = revise_plan(source, [{"operationId": first["operationId"], "title": "Better title"}])
    assert revised.operations[1].target.title == "Better title"
    assert revised.operations[0].before.title == source.operations[0].before.title


def test_project_title_edit_updates_only_proposed_hierarchy_names() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    value["operations"] = [
        {
            "operationId": "rename",
            "action": "update",
            "target": {"type": "project", "id": "parent", "title": "Original project"},
            "before": {"title": "Original project"},
            "after": {"title": "Old proposal"},
            "reason": "Clarify scope",
        },
        {
            "operationId": "move",
            "action": "update",
            "target": {"type": "task", "id": "child", "title": "Child task"},
            "before": {"parent": None},
            "after": {"parent": {"id": "parent", "title": "Old proposal"}},
            "reason": "Group work",
            "dependsOnOperations": ["rename"],
            "display": {
                "beforePath": [],
                "afterPath": [{"id": "parent", "type": "project", "title": "Old proposal"}],
            },
        },
    ]
    source = parse_plan_bytes(json.dumps(value).encode())
    revised = revise_plan(source, [{"operationId": "rename", "title": "Better project name"}])
    assert revised.operations[1].display.afterPath[0].title == "Better project name"
    assert revised.operations[1].after.parent.title == "Better project name"
    assert revised.operations[1].before.parent is None
    assert revised.operations[0].before.title == "Original project"
    with pytest.raises(PlanSemanticError, match="dependency"):
        revise_plan(source, [{"operationId": "rename", "exclude": True}])


def test_title_noop_and_completion_noop_are_rejected() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    source = parse_plan_bytes(json.dumps(value).encode())
    with pytest.raises(PlanSemanticError, match="does not change"):
        revise_plan(
            source,
            [{"operationId": "create-email-follow-up", "title": source.operations[2].after.title}],
        )
    value["operations"] = [
        {
            "operationId": "done",
            "action": "complete",
            "target": {"type": "task", "id": "task", "title": "Task"},
            "completedAt": "2026-08-07T18:00:00Z",
            "reason": "Done",
        }
    ]
    source = parse_plan_bytes(json.dumps(value).encode())
    with pytest.raises(PlanSemanticError, match="does not change"):
        revise_plan(source, [{"operationId": "done", "completedAt": "2026-08-07T18:00:00Z"}])
