from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from marvin_pilot.compiler import compile_create, compile_update
from marvin_pilot.errors import LivePreconditionError, PlanSemanticError, PlanSyntaxError
from marvin_pilot.field_registry import compile_recurring_task_field
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.preflight import preflight_plan
from marvin_pilot.visualizer import build_plan_view

NOW_MS = 1_786_233_600_123
SERIES_ID = "series-daily-id"
OCCURRENCE_ID = "2026-08-15-series-daily-id"


class FakeReader:
    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = copy.deepcopy(documents)
        self.calls: list[str] = []

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        self.calls.append(item_id)
        value = self.documents.get(item_id)
        return copy.deepcopy(value) if value is not None else None

    def get_labels(self) -> list[dict[str, Any]]:
        return []


def plan(operations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "createdAt": "2026-08-15T20:00:00-07:00",
        "summary": "Exercise explicit recurrence scopes.",
        "operations": operations,
    }


def create_series() -> dict[str, Any]:
    return {
        "operationId": "create-daily-series",
        "action": "create",
        "target": {
            "type": "recurringTask",
            "id": "11111111-1111-4111-8111-111111111111",
        },
        "reason": "Create a disposable daily recurrence.",
        "after": {
            "title": "Pilot recurrence fixture",
            "estimatedTimeDuration": "10m",
            "note": "Disposable recurrence fixture.",
            "subtasks": [
                {"id": "sub-a", "title": "First step"},
                {"id": "sub-b", "title": "Second step"},
            ],
            "cadence": {
                "type": "daily",
                "startDate": "2026-08-15",
                "endDate": "2026-08-17",
            },
        },
    }


def update_series() -> dict[str, Any]:
    before_cadence = {
        "type": "daily",
        "startDate": "2026-08-15",
        "endDate": None,
    }
    after_cadence = {
        "type": "n per week",
        "startDate": "2026-08-15",
        "endDate": "2026-09-30",
        "weekdays": [2, 6],
    }
    return {
        "operationId": "update-daily-series",
        "action": "update",
        "target": {"type": "recurringTask", "id": SERIES_ID, "title": "Daily fixture"},
        "reason": "Change the complete series cadence.",
        "before": {"title": "Daily fixture", "cadence": before_cadence, "dueInDays": 0},
        "after": {"title": "Weekly fixture", "cadence": after_cadence, "dueInDays": None},
    }


def occurrence_target(*, series_id: str = SERIES_ID, date: str = "2026-08-15") -> dict:
    return {
        "type": "task",
        "id": OCCURRENCE_ID,
        "title": "Daily fixture",
        "recurrence": {
            "scope": "occurrence",
            "seriesId": series_id,
            "seriesTitle": "Daily fixture",
            "scheduledDate": date,
        },
    }


def live_documents() -> dict[str, dict[str, Any]]:
    return {
        SERIES_ID: {
            "_id": SERIES_ID,
            "_rev": "1-series",
            "db": "RecurringTasks",
            "recurringType": "task",
            "title": "Daily fixture",
            "type": "daily",
            "repeatStart": "2026-08-15",
            "endDate": None,
            "dueIn": 0,
            "updatedAt": 100,
        },
        OCCURRENCE_ID: {
            "_id": OCCURRENCE_ID,
            "_rev": "1-occurrence",
            "db": "Tasks",
            "title": "Daily fixture",
            "day": "2026-08-15",
            "done": False,
            "recurring": True,
            "recurringTaskId": SERIES_ID,
            "updatedAt": 200,
        },
    }


def parse(value: dict[str, Any]):
    return parse_plan_bytes(json.dumps(value).encode())


def test_recurring_series_create_compiles_native_template_and_ordered_subtasks() -> None:
    operation = parse(plan([create_series()])).operations[0]
    compiled = compile_create(operation, NOW_MS)

    assert compiled.endpoint == "doc/create"
    assert compiled.payload["db"] == "RecurringTasks"
    assert compiled.payload["recurringType"] == "task"
    assert compiled.payload["type"] == "daily"
    assert compiled.payload["repeatStart"] == "2026-08-15"
    assert compiled.payload["endDate"] == "2026-08-17"
    assert compiled.payload["subtaskList"] == [
        {"_id": "sub-a", "title": "First step"},
        {"_id": "sub-b", "title": "Second step"},
    ]
    assert compiled.payload["timeEstimate"] == 600_000
    assert compiled.desired_fields["db"] == "RecurringTasks"


@pytest.mark.parametrize(
    ("cadence", "expected"),
    [
        (
            {"type": "daily", "startDate": "2026-08-15", "endDate": None},
            {"type": "daily", "repeatStart": "2026-08-15", "endDate": None},
        ),
        (
            {"type": "weekly", "startDate": "2026-08-15", "weekday": 5},
            {"type": "weekly", "repeatStart": "2026-08-15", "endDate": None, "day": 5},
        ),
        (
            {
                "type": "monthly",
                "startDate": "2026-08-15",
                "monthDate": 31,
                "limitToWeekdays": True,
            },
            {
                "type": "monthly",
                "repeatStart": "2026-08-15",
                "endDate": None,
                "date": 31,
                "limitToWeekdays": True,
            },
        ),
        (
            {"type": "n per week", "startDate": "2026-08-15", "weekdays": [2, 6]},
            {
                "type": "n per week",
                "repeatStart": "2026-08-15",
                "endDate": None,
                "weekDays": [2, 6],
            },
        ),
        (
            {"type": "repeat month", "startDate": "2026-08-15", "interval": 2},
            {
                "type": "repeat month",
                "repeatStart": "2026-08-15",
                "endDate": None,
                "repeat": 2,
                "limitToWeekdays": False,
            },
        ),
        (
            {"type": "echo", "startDate": "2026-08-15", "daysAfterCompletion": 3},
            {
                "type": "echo",
                "repeatStart": "2026-08-15",
                "endDate": None,
                "echoDays": 3,
            },
        ),
        (
            {"type": "onOff", "startDate": "2026-08-15", "onDays": 7, "offDays": 2},
            {
                "type": "onOff",
                "repeatStart": "2026-08-15",
                "endDate": None,
                "onCount": 7,
                "offCount": 2,
            },
        ),
        (
            {
                "type": "custom",
                "startDate": "2026-08-15",
                "expression": "month on the last friday",
            },
            {
                "type": "custom",
                "repeatStart": "2026-08-15",
                "endDate": None,
                "customRecurrence": "month on the last friday",
            },
        ),
    ],
)
def test_every_supported_cadence_compiles_to_documented_native_fields(
    cadence: dict[str, Any], expected: dict[str, Any]
) -> None:
    value = create_series()
    value["after"]["cadence"] = cadence
    parsed = parse(plan([value]))
    normalized = parsed.operations[0].after.model_dump(exclude_unset=True, mode="json")["cadence"]
    assert compile_recurring_task_field("cadence", normalized) == expected


def test_recurring_series_update_compiles_all_cadence_fields_and_snapshots() -> None:
    operation = parse(plan([update_series()])).operations[0]
    live = live_documents()[SERIES_ID]
    compiled = compile_update(operation, live, NOW_MS)
    setters = {item["key"]: item["val"] for item in compiled.payload["setters"]}

    assert setters["title"] == "Weekly fixture"
    assert setters["type"] == "n per week"
    assert setters["weekDays"] == [2, 6]
    assert setters["repeatStart"] == "2026-08-15"
    assert setters["endDate"] == "2026-09-30"
    assert setters["dueIn"] is None
    assert compiled.before_fields["type"] == {"present": True, "value": "daily"}
    assert compiled.before_fields["weekDays"] == {"present": False}


def test_generated_occurrence_requires_and_verifies_exact_series_identity() -> None:
    complete = {
        "operationId": "complete-one-occurrence",
        "action": "complete",
        "target": occurrence_target(),
        "reason": "Complete exactly this historical occurrence.",
        "completedAt": "2026-08-15T08:00:00-07:00",
    }
    documents = live_documents()
    result = preflight_plan(parse(plan([complete])), FakeReader(documents), now_ms=NOW_MS)
    assert result.operations[0].compiled.desired_fields["done"] is True

    no_scope = copy.deepcopy(complete)
    del no_scope["target"]["recurrence"]
    with pytest.raises(LivePreconditionError, match=r"add target\.recurrence"):
        preflight_plan(parse(plan([no_scope])), FakeReader(documents), now_ms=NOW_MS)

    wrong_series = copy.deepcopy(complete)
    wrong_series["target"] = occurrence_target(series_id="wrong-series")
    with pytest.raises(LivePreconditionError, match="series ID is stale"):
        preflight_plan(parse(plan([wrong_series])), FakeReader(documents), now_ms=NOW_MS)

    wrong_date = copy.deepcopy(complete)
    wrong_date["target"] = occurrence_target(date="2026-08-16")
    with pytest.raises(LivePreconditionError, match="occurrence date is stale"):
        preflight_plan(parse(plan([wrong_date])), FakeReader(documents), now_ms=NOW_MS)


def test_occurrence_declaration_is_rejected_for_an_ordinary_task() -> None:
    operation = {
        "operationId": "trash-fake-occurrence",
        "action": "trash",
        "target": occurrence_target(),
        "reason": "Must not weaken ordinary task identity checks.",
    }
    documents = live_documents()
    documents[OCCURRENCE_ID].pop("recurring")
    documents[OCCURRENCE_ID].pop("recurringTaskId")
    with pytest.raises(LivePreconditionError, match="not recurring"):
        preflight_plan(parse(plan([operation])), FakeReader(documents), now_ms=NOW_MS)


def test_completion_coupled_echo_occurrence_remains_blocked() -> None:
    operation = {
        "operationId": "complete-echo-occurrence",
        "action": "complete",
        "target": occurrence_target(),
        "reason": "Must not create the next echo occurrence as an implicit side effect.",
        "completedAt": "2026-08-15T08:00:00-07:00",
    }
    documents = live_documents()
    documents[OCCURRENCE_ID]["echo"] = True
    with pytest.raises(LivePreconditionError, match="recurrence/echo"):
        preflight_plan(parse(plan([operation])), FakeReader(documents), now_ms=NOW_MS)


def test_recurrence_schema_and_semantics_reject_ambiguous_or_invalid_series_changes() -> None:
    invalid = create_series()
    invalid["after"]["cadence"] = {
        "type": "n per week",
        "startDate": "2026-08-15",
        "weekdays": [2, 2],
    }
    with pytest.raises(PlanSyntaxError, match="unique weekday"):
        parse(plan([invalid]))

    complete_series = {
        "operationId": "complete-series",
        "action": "complete",
        "target": {"type": "recurringTask", "id": SERIES_ID, "title": "Daily fixture"},
        "reason": "A series cannot be completed.",
        "completedAt": "2026-08-15T08:00:00-07:00",
    }
    with pytest.raises(PlanSemanticError, match="complete an explicit generated occurrence"):
        parse(plan([complete_series]))

    task_with_cadence = update_series()
    task_with_cadence["target"]["type"] = "task"
    with pytest.raises(PlanSemanticError, match="recurrence-series field"):
        parse(plan([task_with_cadence]))

    series_with_task_subtask_state = update_series()
    series_with_task_subtask_state["before"] = {
        "subtasks": [{"id": "step-a", "title": "Step A", "done": False}]
    }
    series_with_task_subtask_state["after"] = {
        "subtasks": [{"id": "step-a", "title": "Step A", "done": True}]
    }
    with pytest.raises(PlanSemanticError, match=r"invalid before fields.*subtasks"):
        parse(plan([series_with_task_subtask_state]))


def test_visualizer_exposes_series_and_occurrence_scope() -> None:
    occurrence = {
        "operationId": "trash-one-occurrence",
        "action": "trash",
        "target": occurrence_target(),
        "reason": "Remove one generated occurrence.",
    }
    view = build_plan_view(parse(plan([update_series(), occurrence])))
    series, one = view.operations
    assert series.target_type == "recurringTask"
    assert series.recurrence_scope == "series"
    assert series.recurrence_series_id == SERIES_ID
    assert one.target_type == "task"
    assert one.recurrence_scope == "occurrence"
    assert one.recurrence_scheduled_date == "2026-08-15"
