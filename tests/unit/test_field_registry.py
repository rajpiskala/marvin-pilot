from __future__ import annotations

import pytest

from marvin_pilot.field_registry import (
    FIELD_SPECS,
    compile_plan_field,
    field_snapshot,
    live_field_matches,
)


@pytest.mark.parametrize(
    ("field", "plan_value", "marvin_field", "marvin_value"),
    [
        ("title", "New", "title", "New"),
        ("parent", {"id": "people", "title": "People"}, "parentId", "people"),
        ("parent", None, "parentId", "unassigned"),
        ("scheduledDate", "2026-08-09", "day", "2026-08-09"),
        ("scheduledDate", None, "day", "unassigned"),
        ("labels", [{"id": "math", "title": "Math"}], "labelIds", ["math"]),
        ("labels", None, "labelIds", []),
        ("estimatedTimeDuration", "1h30m", "timeEstimate", 5_400_000),
        ("estimatedTimeDuration", None, "timeEstimate", None),
        ("starPriority", "red", "isStarred", 3),
        ("frogSize", "baby", "isFrogged", 2),
        ("snoozedUntil", "1970-01-01T00:01:00Z", "itemSnoozeTime", 60_000),
        ("dependencies", ["a", "b"], "dependsOn", {"a": True, "b": True}),
        ("dependencies", None, "dependsOn", {}),
    ],
)
def test_field_compilation(
    field: str, plan_value: object, marvin_field: str, marvin_value: object
) -> None:
    assert compile_plan_field(field, plan_value) == (marvin_field, marvin_value)


def test_registry_covers_exact_plan_field_allowlist() -> None:
    assert list(FIELD_SPECS) == [
        "title",
        "parent",
        "scheduledDate",
        "dueDate",
        "startDate",
        "endDate",
        "plannedWeek",
        "plannedMonth",
        "labels",
        "estimatedTimeDuration",
        "note",
        "dayRank",
        "masterRank",
        "dailySection",
        "bonusSection",
        "customSectionId",
        "timeBlockSectionId",
        "starPriority",
        "frogSize",
        "backburner",
        "reviewDate",
        "snoozedUntil",
        "permanentSnoozeUntil",
        "dependencies",
    ]


@pytest.mark.parametrize(
    ("field", "expected", "document"),
    [
        ("parent", {"id": "unassigned", "title": "Inbox"}, {"parentId": "unassigned"}),
        ("parent", None, {}),
        ("scheduledDate", None, {}),
        ("scheduledDate", None, {"day": None}),
        ("labels", [], {}),
        ("labels", [{"id": "a", "title": "A"}], {"labelIds": ["a"]}),
        ("dependencies", [], {}),
        ("dependencies", ["a"], {"dependsOn": {"a": True, "ignored": False}}),
        ("estimatedTimeDuration", None, {}),
    ],
)
def test_live_comparison_uses_semantic_storage(
    field: str, expected: object, document: dict
) -> None:
    assert live_field_matches(field, expected, document)


def test_live_comparison_detects_mismatch() -> None:
    assert not live_field_matches("scheduledDate", "2026-08-09", {"day": "2026-08-08"})
    assert not live_field_matches("parent", {"id": "people"}, {"parentId": "work"})


def test_field_snapshot_distinguishes_missing_and_null() -> None:
    assert field_snapshot({}, "note") == {"present": False}
    assert field_snapshot({"note": None}, "note") == {"present": True, "value": None}
    assert field_snapshot({"note": "hello"}, "note") == {
        "present": True,
        "value": "hello",
    }
