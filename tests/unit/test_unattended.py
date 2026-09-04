from __future__ import annotations

import copy
import json

import pytest

from marvin_pilot.errors import PlanSemanticError
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.preflight import preflight_plan
from marvin_pilot.unattended import assess_unattended, enforce_unattended


class FakeReader:
    def __init__(self, documents: dict[str, dict] | None = None) -> None:
        self.documents = copy.deepcopy(documents or {})

    def get_doc(self, item_id: str) -> dict | None:
        document = self.documents.get(item_id)
        return copy.deepcopy(document) if document is not None else None

    def get_labels(self) -> list[dict]:
        return []


def checked(operations: list[dict], documents: dict[str, dict] | None = None):
    raw = json.dumps(
        {
            "schemaVersion": 1,
            "planId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "createdAt": "2026-09-04T12:00:00-07:00",
            "summary": "Exercise bounded unattended apply policy.",
            "operations": operations,
        }
    ).encode()
    return preflight_plan(parse_plan_bytes(raw), FakeReader(documents), now_ms=1_788_552_000_000)


def task_create(*, subtasks: int = 0) -> dict:
    return {
        "operationId": "create-task",
        "action": "create",
        "target": {"type": "task", "id": "11111111-1111-4111-8111-111111111111"},
        "reason": "Create a bounded task fixture.",
        "after": {
            "title": "Prepare dinner",
            "subtasks": [
                {"id": f"step-{index}", "title": f"Step {index}"} for index in range(subtasks)
            ],
        },
    }


def test_impact_counts_documents_and_embedded_subtasks() -> None:
    result = checked([task_create(subtasks=4)])
    assessment = assess_unattended(result)
    assert assessment.eligible
    assert assessment.impact == 5


def test_task_trash_counts_the_deleted_checklist() -> None:
    operation = {
        "operationId": "trash-task",
        "action": "trash",
        "target": {"type": "task", "id": "task-1", "title": "Prepare dinner"},
        "reason": "Remove a disposable task fixture.",
    }
    live = {
        "task-1": {
            "_id": "task-1",
            "db": "Tasks",
            "title": "Prepare dinner",
            "done": False,
            "updatedAt": 1,
            "subtasks": {
                f"step-{index}": {"_id": f"step-{index}", "title": f"Step {index}"}
                for index in range(3)
            },
        }
    }
    assert assess_unattended(checked([operation], live)).impact == 4


def test_project_trash_and_recurring_series_require_interactive_review() -> None:
    project_trash = {
        "operationId": "trash-project",
        "action": "trash",
        "target": {"type": "project", "id": "project-1", "title": "Old project"},
        "reason": "Exercise the container safety boundary.",
    }
    project_live = {
        "project-1": {
            "_id": "project-1",
            "db": "Categories",
            "type": "project",
            "title": "Old project",
            "done": False,
            "updatedAt": 1,
        }
    }
    project = assess_unattended(checked([project_trash], project_live))
    assert not project.eligible
    assert "trashes a container" in project.blockers[0]

    recurring_create = {
        "operationId": "create-series",
        "action": "create",
        "target": {
            "type": "recurringTask",
            "id": "22222222-2222-4222-8222-222222222222",
        },
        "reason": "Exercise the recurrence safety boundary.",
        "after": {
            "title": "Daily fixture",
            "cadence": {"type": "daily", "startDate": "2026-09-04"},
        },
    }
    recurring = assess_unattended(checked([recurring_create]))
    assert not recurring.eligible
    assert "recurring-task series" in recurring.blockers[0]


def test_enforcement_reports_configured_impact_limit() -> None:
    result = checked([task_create(subtasks=4)])
    with pytest.raises(PlanSemanticError, match=r"plan impact is 5.*maximum is 4"):
        enforce_unattended(result, max_impact=4)
    assert enforce_unattended(result, max_impact=5).impact == 5
