from __future__ import annotations

import json

from marvin_pilot.describe import render_plan_description
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.preflight import preflight_plan


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


def test_description_calls_out_explicitly_accepted_source_task_loss() -> None:
    value = {
        "schemaVersion": 1,
        "planId": "55555555-5555-4555-8555-555555555555",
        "createdAt": "2026-08-09T08:00:00-07:00",
        "summary": "Describe acknowledged source-task loss.",
        "operations": [
            {
                "operationId": "build-checklist",
                "action": "update",
                "target": {"type": "task", "id": "parent", "title": "Parent task"},
                "reason": "Consolidate one source task.",
                "before": {"subtasks": []},
                "after": {
                    "subtasks": [
                        {
                            "id": "sub-a",
                            "title": "Source task",
                            "sourceTask": {
                                "id": "source-a",
                                "title": "Source task",
                                "acceptLoss": ["estimatedTimeDuration", "note"],
                            },
                        }
                    ]
                },
            },
            {
                "operationId": "trash-source",
                "action": "trash",
                "target": {"type": "task", "id": "source-a", "title": "Source task"},
                "reason": "The subtask replaces the source.",
                "dependsOnOperations": ["build-checklist"],
            },
        ],
    }

    rendered = render_plan_description(parse_plan_bytes(json.dumps(value).encode()))

    assert (
        "explicitly accepted sourceTask loss: Source task [source-a] -> "
        "estimated time duration, note"
    ) in rendered


def test_live_description_surfaces_preflight_and_compiler_managed_fields() -> None:
    from marvin_pilot.describe import render_live_preflight

    class Reader:
        def __init__(self) -> None:
            self.documents = {
                "task-wash-dishes-id": {
                    "db": "Tasks",
                    "title": "Wash the dishes",
                    "day": "2026-08-08",
                },
            }

        def get_doc(self, item_id: str):
            return self.documents.get(item_id)

    value = json.loads(json.dumps(EXAMPLE_PLAN))
    value["operations"] = [value["operations"][0]]
    plan = parse_plan_bytes(json.dumps(value).encode())
    result = preflight_plan(plan, Reader(), now_ms=123)
    rendered = render_live_preflight(result)
    assert "Live preflight: PASSED for 1 operation(s)" in rendered
    assert "Strict concurrency recheck: on" in rendered
    assert "Compiler-managed [reschedule-wash-dishes]: firstScheduled=2026-08-09" in rendered
