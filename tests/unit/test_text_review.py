from __future__ import annotations

import copy
import json

import pytest

from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import canonical_plan_bytes, parse_plan_bytes
from marvin_pilot.text_review import build_text_review, text_choices


def sample():
    value = copy.deepcopy(EXAMPLE_PLAN)
    operation = next(o for o in value["operations"] if o["action"] == "create")
    operation["after"]["subtasks"] = [
        {"id": "stable-mobile", "title": "Test mobile layout", "done": False},
        {"id": "stable-other", "title": "Test Android layout", "done": True},
    ]
    return parse_plan_bytes(json.dumps(value).encode()), operation["operationId"]


def test_text_syntax_and_subtask_identity_are_preserved_without_revising_plan():
    plan, identifier = sample()
    before = canonical_plan_bytes(plan)
    review = build_text_review(
        plan,
        [
            {
                "operationId": identifier,
                "field": "title",
                "text": "Website Launch v3 (2026) +4d due Nov 30",
            },
            {
                "operationId": identifier,
                "field": "subtaskTitle",
                "subtaskId": "stable-mobile",
                "text": "Test iPhone layout due Nov 30",
            },
        ],
        context={"timeZone": "America/Los_Angeles", "localDate": "2026-11-11"},
    )
    assert canonical_plan_bytes(plan) == before
    assert review["requiresAIReconciliation"] is True
    assert review["edits"][0]["text"].endswith("+4d due Nov 30")
    assert review["edits"][1]["subtaskId"] == "stable-mobile"
    assert review["edits"][1]["originalText"] == "Test mobile layout"
    assert review["edits"][0]["action"] == "create"
    assert review["edits"][0]["targetId"] == plan.operations[2].target.id
    assert {c["subtaskId"] for c in text_choices(plan) if c["field"] == "subtaskTitle"} == {
        "stable-mobile",
        "stable-other",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"exclude": True},
        {"action": "trash"},
        {"done": True},
        {"completedAt": "2026-11-11T12:00:00Z"},
        {"subtasks": []},
        {"targetId": "another"},
    ],
)
def test_structural_and_state_edits_are_rejected(extra):
    plan, identifier = sample()
    with pytest.raises(PlanSyntaxError):
        build_text_review(
            plan,
            [{"operationId": identifier, "field": "title", "text": "Changed", **extra}],
            context={"timeZone": "UTC", "localDate": "2026-11-11"},
        )


@pytest.mark.parametrize(
    "edit",
    [
        {"field": "title", "text": " "},
        {"field": "subtaskTitle", "subtaskId": "wrong-id", "text": "Changed"},
        {"field": "title", "subtaskId": "stable-mobile", "text": "Changed"},
    ],
)
def test_blank_or_wrong_exact_item_is_rejected(edit):
    plan, identifier = sample()
    with pytest.raises(PlanSemanticError):
        build_text_review(
            plan,
            [{"operationId": identifier, **edit}],
            context={"timeZone": "UTC", "localDate": "2026-11-11"},
        )
