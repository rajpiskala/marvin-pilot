from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from marvin_pilot.executor import execute_apply
from marvin_pilot.history import HistoryStore
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.reverter import execute_revert

NOW_MS = 1_786_233_600_123
SERIES_UPDATE = "series-update"
SERIES_TRASH = "series-trash"


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=10)
        return current


class InMemoryMarvin:
    api_base_host = "https://marvin.test"

    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = copy.deepcopy(documents)

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        value = self.documents.get(item_id)
        return copy.deepcopy(value) if value is not None else None

    def get_labels(self) -> list[dict[str, Any]]:
        return []

    def update_doc(self, item_id: str, setters: list[dict[str, Any]]) -> dict[str, bool]:
        document = self.documents[item_id]
        for setter in setters:
            key = setter["key"]
            if key.startswith("fieldUpdates."):
                document.setdefault("fieldUpdates", {})[key.split(".", 1)[1]] = setter["val"]
            else:
                document[key] = setter["val"]
        document["_rev"] = "next-update"
        return {"ok": True}

    def create_doc(self, document: dict[str, Any]) -> dict[str, bool]:
        created = copy.deepcopy(document)
        created["_rev"] = "1-created"
        self.documents[document["_id"]] = created
        return {"ok": True}

    def delete_doc(self, item_id: str) -> dict[str, bool]:
        self.documents.pop(item_id)
        return {"ok": True}


def recurrence_ref(series_id: str, title: str, date: str) -> dict[str, str]:
    return {
        "scope": "occurrence",
        "seriesId": series_id,
        "seriesTitle": title,
        "scheduledDate": date,
    }


def occurrence(identifier: str, date: str) -> dict[str, Any]:
    return {
        "_id": identifier,
        "_rev": "1-occurrence",
        "db": "Tasks",
        "title": "Existing recurring fixture",
        "day": date,
        "done": False,
        "recurring": True,
        "recurringTaskId": SERIES_UPDATE,
        "updatedAt": 100,
    }


def documents() -> dict[str, dict[str, Any]]:
    return {
        SERIES_UPDATE: {
            "_id": SERIES_UPDATE,
            "_rev": "1-series",
            "db": "RecurringTasks",
            "recurringType": "task",
            "title": "Existing recurring fixture",
            "type": "daily",
            "repeatStart": "2026-08-15",
            "endDate": None,
            "deletedDates": ["2026-08-01"],
            "subtaskList": [],
            "updatedAt": 100,
        },
        SERIES_TRASH: {
            "_id": SERIES_TRASH,
            "_rev": "1-series-trash",
            "db": "RecurringTasks",
            "recurringType": "task",
            "title": "Redundant recurring fixture",
            "type": "weekly",
            "day": 1,
            "repeatStart": "2026-08-15",
            "endDate": None,
            "deletedDates": ["2026-08-08"],
            "updatedAt": 100,
        },
        "occurrence-update": occurrence("occurrence-update", "2026-08-15"),
        "occurrence-complete": occurrence("occurrence-complete", "2026-08-16"),
        "occurrence-trash": occurrence("occurrence-trash", "2026-08-17"),
    }


def plan_value() -> dict[str, Any]:
    new_id = "22222222-2222-4222-8222-222222222222"
    return {
        "schemaVersion": 1,
        "planId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "createdAt": "2026-08-15T20:00:00-07:00",
        "summary": "Exercise recurring series and generated occurrence execution.",
        "operations": [
            {
                "operationId": "create-series",
                "action": "create",
                "target": {"type": "recurringTask", "id": new_id},
                "reason": "Create a disposable recurrence series.",
                "after": {
                    "title": "New recurring fixture",
                    "subtasks": [{"id": "step-a", "title": "First step"}],
                    "cadence": {
                        "type": "weekly",
                        "startDate": "2026-08-15",
                        "weekday": 6,
                        "endDate": "2026-08-29",
                    },
                },
            },
            {
                "operationId": "update-series",
                "action": "update",
                "target": {
                    "type": "recurringTask",
                    "id": SERIES_UPDATE,
                    "title": "Existing recurring fixture",
                },
                "reason": "Rename and add future subtask steps.",
                "before": {"title": "Existing recurring fixture", "subtasks": []},
                "after": {
                    "title": "Updated recurring fixture",
                    "subtasks": [
                        {"id": "step-1", "title": "Order"},
                        {"id": "step-2", "title": "Pick up"},
                    ],
                },
            },
            {
                "operationId": "update-occurrence",
                "action": "update",
                "target": {
                    "type": "task",
                    "id": "occurrence-update",
                    "title": "Existing recurring fixture",
                    "recurrence": recurrence_ref(
                        SERIES_UPDATE, "Updated recurring fixture", "2026-08-15"
                    ),
                },
                "reason": "Edit this occurrence only.",
                "dependsOnOperations": ["update-series"],
                "before": {"note": None},
                "after": {"note": "Occurrence-only note"},
            },
            {
                "operationId": "complete-occurrence",
                "action": "complete",
                "target": {
                    "type": "task",
                    "id": "occurrence-complete",
                    "title": "Existing recurring fixture",
                    "recurrence": recurrence_ref(
                        SERIES_UPDATE, "Updated recurring fixture", "2026-08-16"
                    ),
                },
                "reason": "Complete this historical occurrence only.",
                "dependsOnOperations": ["update-series"],
                "completedAt": "2026-08-15T09:00:00-07:00",
            },
            {
                "operationId": "trash-occurrence",
                "action": "trash",
                "target": {
                    "type": "task",
                    "id": "occurrence-trash",
                    "title": "Existing recurring fixture",
                    "recurrence": recurrence_ref(
                        SERIES_UPDATE, "Updated recurring fixture", "2026-08-17"
                    ),
                },
                "reason": "Trash one generated occurrence only.",
                "dependsOnOperations": ["update-series"],
            },
            {
                "operationId": "trash-series",
                "action": "trash",
                "target": {
                    "type": "recurringTask",
                    "id": SERIES_TRASH,
                    "title": "Redundant recurring fixture",
                },
                "reason": "Trash the entire redundant series.",
            },
        ],
    }


def test_recurring_crud_apply_receipts_and_reverse_revert(tmp_path: Path) -> None:
    raw = json.dumps(plan_value(), indent=2).encode()
    plan = parse_plan_bytes(raw)
    client = InMemoryMarvin(documents())
    clock = Clock()
    history = HistoryStore(tmp_path, now=clock)

    applied = execute_apply(
        plan,
        raw,
        client=client,
        history=history,
        approve=lambda _checked: True,
        now_ms=lambda: NOW_MS,
        wall_clock=clock,
    )

    new_id = plan.operations[0].target.id
    assert client.documents[new_id]["db"] == "RecurringTasks"
    assert client.documents[new_id]["type"] == "weekly"
    assert client.documents[SERIES_UPDATE]["title"] == "Updated recurring fixture"
    assert client.documents[SERIES_UPDATE]["subtaskList"] == [
        {"_id": "step-1", "title": "Order"},
        {"_id": "step-2", "title": "Pick up"},
    ]
    assert client.documents["occurrence-update"]["note"] == "Occurrence-only note"
    assert client.documents["occurrence-complete"]["done"] is True
    assert "occurrence-trash" not in client.documents
    assert SERIES_TRASH not in client.documents
    assert client.documents[SERIES_UPDATE]["deletedDates"] == ["2026-08-01"]
    assert applied.receipt.operations[0].targetType == "recurringTask"
    assert applied.receipt.operations[2].recurrence is not None
    assert applied.receipt.operations[2].recurrence.scope == "occurrence"

    reverted = execute_revert(
        applied.receipt,
        Path(applied.receipt_path),
        [],
        client=client,
        history=history,
        approve=lambda _checked: True,
        now_ms=lambda: NOW_MS + 60_000,
        wall_clock=clock,
    )

    assert reverted.receipt.status == "reverted"
    assert new_id not in client.documents
    assert client.documents[SERIES_UPDATE]["title"] == "Existing recurring fixture"
    assert client.documents[SERIES_UPDATE]["subtaskList"] == []
    assert client.documents["occurrence-update"]["note"] is None
    assert client.documents["occurrence-complete"]["done"] is False
    assert client.documents["occurrence-trash"]["title"] == "Existing recurring fixture"
    assert "deletedAt" not in client.documents["occurrence-trash"]
    assert client.documents[SERIES_TRASH]["title"] == "Redundant recurring fixture"
    assert client.documents[SERIES_TRASH]["deletedDates"] == ["2026-08-08"]
    assert "deletedAt" not in client.documents[SERIES_TRASH]
