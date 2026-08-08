from __future__ import annotations

import json

from marvin_pilot.describe import render_plan_description
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import parse_plan_bytes


def test_description_contains_review_critical_information() -> None:
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    rendered = render_plan_description(plan)

    assert "Plan ID: 5f97946d-35f3-4a52-84de-4fe51e694788" in rendered
    assert "Digest: sha256:" in rendered
    assert '1. UPDATE "Wash the dishes"' in rendered
    assert "scheduled date: 2026-08-08 -> 2026-08-09" in rendered
    assert "estimated time duration: none -> 4h30m" in rendered
    assert "parent: Inbox [unassigned] -> People [people-category-id]" in rendered
    assert "[create-email-follow-up]" in rendered
    assert "expected updatedAt: 1786221000123" in rendered
    assert "Totals: 1 create, 2 updates, 1 trash" in rendered


def test_none_is_only_prose_not_quoted() -> None:
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    rendered = render_plan_description(plan)
    assert "estimated time duration: none -> 4h30m" in rendered
    assert '"none"' not in rendered


def test_description_formats_lists_booleans_and_dependencies() -> None:
    value = json.loads(json.dumps(EXAMPLE_PLAN))
    operation = value["operations"][1]
    operation["before"] = {
        "labels": [],
        "dependencies": [],
        "backburner": None,
    }
    operation["after"] = {
        "labels": [{"id": "label-a", "title": "Math"}],
        "dependencies": ["task-a", "task-b"],
        "backburner": True,
    }
    operation["dependsOnOperations"] = ["reschedule-wash-dishes"]
    plan = parse_plan_bytes(json.dumps(value).encode())
    rendered = render_plan_description(plan)
    assert "labels: none -> Math [label-a]" in rendered
    assert "dependencies: none -> task-a, task-b" in rendered
    assert "backburner: none -> yes" in rendered
    assert "depends on: reschedule-wash-dishes" in rendered
