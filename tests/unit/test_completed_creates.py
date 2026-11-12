from __future__ import annotations

import copy
import json
from datetime import datetime
from io import StringIO

import pytest
from rich.console import Console

from marvin_pilot.compiler import compile_create
from marvin_pilot.describe import (
    plan_description_payload,
    render_plan_description,
    render_plan_markdown,
)
from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import canonical_plan_bytes, parse_plan_bytes
from marvin_pilot.preflight import preflight_plan
from marvin_pilot.prepare import SnapshotReader, parse_draft_bytes, prepare_draft
from marvin_pilot.terminal_review import render_live_preflight_terminal
from marvin_pilot.visualizer import build_plan_view

COMPLETED_AT = "2026-08-07T23:30:00-07:00"
DONE_MS = int(datetime.fromisoformat(COMPLETED_AT).timestamp() * 1000)


def completed_create_value() -> dict:
    value = copy.deepcopy(EXAMPLE_PLAN)
    operation = value["operations"][2]
    operation["after"].pop("scheduledDate", None)
    operation["completedAt"] = COMPLETED_AT
    value["operations"] = [operation]
    return value


def test_completed_create_is_one_final_state_mutation_and_review() -> None:
    plan = parse_plan_bytes(json.dumps(completed_create_value()).encode())
    compiled = compile_create(plan.operations[0], 1_800_000_000_000)
    assert compiled.endpoint == "doc/create"
    assert compiled.payload["done"] is True
    assert compiled.payload["doneAt"] == DONE_MS
    assert compiled.payload["day"] == "2026-08-07"
    assert "doneDate" not in compiled.payload
    assert "firstScheduled" not in compiled.payload
    assert compiled.payload["updatedAt"] == 1_800_000_000_000
    view = build_plan_view(plan).to_dict()["operations"][0]
    assert view["completed_at"] == COMPLETED_AT
    assert view["completion_day_after"] == "2026-08-07"
    assert view["change_kinds"] == ["created", "completed"]
    assert view["before"] is None


@pytest.mark.parametrize(
    "timestamp", [None, "2026-08-07", "2026-08-07T23:30:00", "2027-01-01T00:00:00Z"]
)
def test_completed_create_rejects_invalid_or_future_time(timestamp) -> None:
    value = completed_create_value()
    value["operations"][0]["completedAt"] = timestamp
    with pytest.raises((PlanSyntaxError, PlanSemanticError)):
        parse_plan_bytes(json.dumps(value).encode())


@pytest.mark.parametrize("kind", ["project", "category", "recurringTask"])
def test_completed_create_only_supports_ordinary_tasks(kind: str) -> None:
    value = completed_create_value()
    value["operations"][0]["target"]["type"] = kind
    with pytest.raises((PlanSyntaxError, PlanSemanticError)):
        parse_plan_bytes(json.dumps(value).encode())


@pytest.mark.parametrize("day", [None, "2026-08-08"])
def test_completed_create_rejects_conflicting_schedule(day) -> None:
    value = completed_create_value()
    value["operations"][0]["after"]["scheduledDate"] = day
    with pytest.raises(PlanSyntaxError, match="scheduledDate"):
        parse_plan_bytes(json.dumps(value).encode())


def test_completed_create_subtask_time_and_matching_explicit_schedule() -> None:
    value = completed_create_value()
    value["operations"][0]["after"].update(
        scheduledDate="2026-08-07",
        subtasks=[
            {"id": "checked", "title": "Finished step", "done": True},
            {"id": "open", "title": "Unfinished step", "done": False},
        ],
    )
    plan = parse_plan_bytes(json.dumps(value).encode())
    document = compile_create(plan.operations[0], 1_800_000_000_000).payload
    assert document["firstScheduled"] == "2026-08-07"
    assert document["subtasks"]["checked"]["doneAt"] == DONE_MS
    assert "doneAt" not in document["subtasks"]["open"]


def test_open_create_wire_shape_is_unchanged() -> None:
    value = completed_create_value()
    del value["operations"][0]["completedAt"]
    plan = parse_plan_bytes(json.dumps(value).encode())
    assert b"completedAt" not in canonical_plan_bytes(plan)
    assert compile_create(plan.operations[0], 123).payload["done"] is False


def test_draft_preserves_completed_create() -> None:
    value = completed_create_value()
    value.pop("schemaVersion")
    value.pop("planId")
    value.pop("createdAt")
    value.pop("$schema", None)
    value["draftVersion"] = 1
    value["expectedAccount"] = {"userId": "123456", "email": "pilot@example.com"}
    value["operations"][0].pop("display", None)
    value["operations"][0]["after"].pop("parent", None)
    draft = parse_draft_bytes(json.dumps(value).encode())
    plan = prepare_draft(draft, SnapshotReader([]), now_ms=1_800_000_000_000)
    assert plan.operations[0].completedAt == COMPLETED_AT


def test_completed_create_descriptions_and_terminal_review_explain_history() -> None:
    plan = parse_plan_bytes(json.dumps(completed_create_value()).encode())
    assert COMPLETED_AT in render_plan_description(plan)
    assert "2026-08-07" in render_plan_markdown(plan)
    payload = plan_description_payload(plan)["operations"][0]
    assert payload["completedAt"] == COMPLETED_AT
    assert payload["completionDay"]["behavior"] == "assigned"
    checked = preflight_plan(plan, SnapshotReader([]), now_ms=1_800_000_000_000)
    output = StringIO()
    console = Console(file=output, width=140, color_system=None)
    console.print(render_live_preflight_terminal(checked, encoding="utf-8"))
    assert COMPLETED_AT in output.getvalue()
    assert "2026-08-07" in output.getvalue()


def test_completed_create_rejects_source_task_consolidation() -> None:
    value = completed_create_value()
    value["operations"][0]["after"]["subtasks"] = [
        {
            "id": "embedded",
            "title": "Converted step",
            "done": True,
            "sourceTask": {"id": "existing", "title": "Original loose task"},
        }
    ]
    with pytest.raises(PlanSyntaxError, match="sourceTask"):
        parse_plan_bytes(json.dumps(value).encode())


def test_completed_create_rejects_recurring_field_shape() -> None:
    value = completed_create_value()
    value["operations"][0]["after"] = {
        "title": "Not an ordinary task",
        "cadence": {"kind": "daily", "interval": 1, "startDate": "2026-08-01"},
    }
    with pytest.raises(PlanSyntaxError):
        parse_plan_bytes(json.dumps(value).encode())
