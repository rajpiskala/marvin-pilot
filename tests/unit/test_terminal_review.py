from __future__ import annotations

import copy
import json
from io import StringIO

import pytest
from rich.console import Console

from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.preflight import PreflightResult, preflight_plan
from marvin_pilot.terminal_review import (
    contrast_foreground,
    render_live_preflight_terminal,
)


class Reader:
    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = documents

    def get_doc(self, item_id: str):
        document = self.documents.get(item_id)
        return copy.deepcopy(document) if document is not None else None


def task_result() -> PreflightResult:
    value = copy.deepcopy(EXAMPLE_PLAN)
    value["summary"] = "Move the household task into tomorrow's plan."
    value["operations"] = [value["operations"][0]]
    value["operations"][0]["display"] = {
        "beforePath": [
            {
                "id": "home-category",
                "type": "category",
                "title": "Home & Household",
                "emoji": "🏠",
                "color": "#C69C7B",
            }
        ],
        "afterPath": [
            {
                "id": "home-category",
                "type": "category",
                "title": "Home & Household",
                "emoji": "🏠",
                "color": "#C69C7B",
            }
        ],
    }
    plan = parse_plan_bytes(json.dumps(value).encode())
    return preflight_plan(
        plan,
        Reader(
            {
                "task-wash-dishes-id": {
                    "_id": "task-wash-dishes-id",
                    "_rev": "1-task",
                    "db": "Tasks",
                    "title": "Wash the dishes",
                    "day": "2026-08-08",
                    "updatedAt": 1,
                }
            }
        ),
        now_ms=123,
    )


@pytest.mark.parametrize(
    "background",
    ["#000000", "#ffffff", "#c69c7b", "#245e8d", "#ffeb3b", "#7e57c2"],
)
def test_marvin_color_chips_choose_accessible_foreground(background: str) -> None:
    foreground, ratio = contrast_foreground(background)
    assert foreground in {"black", "white"}
    assert ratio >= 4.5


def test_terminal_review_is_colored_and_contains_redundant_review_labels() -> None:
    output = StringIO()
    review_console = Console(
        file=output,
        force_terminal=True,
        color_system="truecolor",
        width=140,
    )

    review_console.print(render_live_preflight_terminal(task_result(), encoding="utf-8"))
    rendered = output.getvalue()

    assert "\x1b[" in rendered
    assert "REVIEWED APPLY PLAN" in rendered
    assert "Live preflight: PASSED" in rendered
    assert "UPDATE" in rendered
    assert "[TASK]" in rendered
    assert "Wash the dishes" in rendered
    assert "Operation ID" in rendered
    assert "reschedule-wash-dishes" in rendered
    assert "Target ID" in rendered
    assert "task-wash-dishes-id" in rendered
    assert "Now location" in rendered
    assert "Home & Household" in rendered
    assert "#c69c7b" in rendered
    assert "🏠" in rendered
    assert "scheduled date" in rendered
    assert "2026-08-08" in rendered
    assert "2026-08-09" in rendered
    assert "Reason" in rendered
    assert "Pilot-managed" in rendered


def test_terminal_review_falls_back_to_text_when_encoding_cannot_show_emoji() -> None:
    output = StringIO()
    review_console = Console(file=output, force_terminal=False, width=140)

    review_console.print(render_live_preflight_terminal(task_result(), encoding="cp1252"))
    rendered = output.getvalue()

    assert "[TASK]" in rendered
    assert "[CATEGORY] Home & Household" in rendered
    assert "🏠" not in rendered
    assert "📝" not in rendered


def test_terminal_review_distinguishes_projects_and_recurring_series() -> None:
    value = {
        "schemaVersion": 1,
        "planId": "5c186e31-d14b-448c-a826-5c243eced79c",
        "createdAt": "2026-08-24T12:00:00-07:00",
        "summary": "Rename a project and add a recurring review.",
        "operations": [
            {
                "operationId": "rename-client-project",
                "action": "update",
                "target": {
                    "type": "project",
                    "id": "client-project-id",
                    "title": "Client project",
                },
                "reason": "Make the project outcome explicit.",
                "before": {"title": "Client project"},
                "after": {"title": "Client launch"},
            },
            {
                "operationId": "create-weekly-review-series",
                "action": "create",
                "target": {
                    "type": "recurringTask",
                    "id": "f16094ef-d7b3-492a-b17d-d0b13bb9084f",
                },
                "reason": "Add a weekly review ritual.",
                "after": {
                    "title": "Review the client launch",
                    "cadence": {
                        "type": "weekly",
                        "startDate": "2026-08-24",
                        "weekday": 0,
                    },
                },
            },
        ],
    }
    plan = parse_plan_bytes(json.dumps(value).encode())
    result = preflight_plan(
        plan,
        Reader(
            {
                "client-project-id": {
                    "_id": "client-project-id",
                    "db": "Categories",
                    "type": "project",
                    "title": "Client project",
                    "emoji": "🚀",
                    "color": "#245e8d",
                }
            }
        ),
        now_ms=123,
    )
    output = StringIO()
    Console(file=output, force_terminal=False, width=140).print(
        render_live_preflight_terminal(result, encoding="utf-8")
    )
    rendered = output.getvalue()

    assert "UPDATE  [PROJECT]  🚀  Client project" in rendered
    assert "CREATE  [SERIES]  🔁  Review the client launch" in rendered
    assert "Scope" in rendered
    assert "entire recurring series" in rendered


def test_terminal_review_explains_complete_and_trash_effects() -> None:
    value = {
        "schemaVersion": 1,
        "planId": "d327301b-e2ba-4b89-8b9e-3e1ae053fa49",
        "createdAt": "2026-08-24T12:00:00-07:00",
        "summary": "Close the launch and remove a duplicate.",
        "operations": [
            {
                "operationId": "complete-client-launch",
                "action": "complete",
                "target": {
                    "type": "project",
                    "id": "client-project-id",
                    "title": "Client launch",
                },
                "reason": "The launch has shipped.",
                "completedAt": "2026-08-23T17:30:00-07:00",
            },
            {
                "operationId": "trash-duplicate-task",
                "action": "trash",
                "target": {
                    "type": "task",
                    "id": "duplicate-task-id",
                    "title": "Duplicate follow-up",
                },
                "reason": "The canonical follow-up already exists.",
            },
        ],
    }
    plan = parse_plan_bytes(json.dumps(value).encode())
    result = preflight_plan(
        plan,
        Reader(
            {
                "client-project-id": {
                    "_id": "client-project-id",
                    "db": "Categories",
                    "type": "project",
                    "title": "Client launch",
                    "done": False,
                },
                "duplicate-task-id": {
                    "_id": "duplicate-task-id",
                    "db": "Tasks",
                    "title": "Duplicate follow-up",
                    "done": False,
                },
            }
        ),
        now_ms=123,
    )
    output = StringIO()
    Console(file=output, force_terminal=False, width=140).print(
        render_live_preflight_terminal(result, encoding="utf-8")
    )
    rendered = output.getvalue()

    assert "COMPLETE  [PROJECT]" in rendered
    assert "completion" in rendered
    assert "historical completion timestamp" in rendered
    assert "TRASH  [TASK]" in rendered
    assert "(removed by this plan)" in rendered
    assert "full recovery snapshot is journaled" in rendered
