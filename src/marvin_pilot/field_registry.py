"""The sole allowlisted mapping between plan fields and Marvin task fields."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from marvin_pilot.duration import duration_to_milliseconds

PlanValue = Any
ToMarvin = Callable[[PlanValue], Any]
NormalizeMarvin = Callable[[Any, bool], Any]


def _identity(value: Any) -> Any:
    return value


def _identity_live(value: Any, present: bool) -> Any:
    return value if present else None


def _scheduled_to_marvin(value: str | None) -> str:
    return value if value is not None else "unassigned"


def _scheduled_live(value: Any, present: bool) -> str:
    return value if present and value is not None and value != "" else "unassigned"


def _parent_to_marvin(value: dict[str, Any] | None) -> str:
    return value["id"] if value is not None else "unassigned"


def _parent_live(value: Any, present: bool) -> str:
    return value if present and value is not None and value != "" else "unassigned"


def _labels_to_marvin(value: list[dict[str, Any]] | None) -> list[str]:
    return [] if value is None else [item["id"] for item in value]


def _labels_live(value: Any, present: bool) -> list[str]:
    if not present or value is None:
        return []
    return list(value)


def _estimate_to_marvin(value: str | None) -> int | None:
    return None if value is None else duration_to_milliseconds(value)


def _priority_to_marvin(value: str | None) -> int | None:
    return None if value is None else {"yellow": 1, "orange": 2, "red": 3}[value]


def _frog_to_marvin(value: str | None) -> int | None:
    return None if value is None else {"normal": 1, "baby": 2, "monster": 3}[value]


def _snooze_to_marvin(value: str | None) -> int | None:
    if value is None:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    return int(datetime.fromisoformat(candidate).timestamp() * 1_000)


def _dependencies_to_marvin(value: list[str] | None) -> dict[str, bool]:
    return {} if value is None else dict.fromkeys(value, True)


def _dependencies_live(value: Any, present: bool) -> dict[str, bool]:
    if not present or value is None:
        return {}
    return {key: bool(enabled) for key, enabled in value.items() if enabled}


@dataclass(frozen=True, slots=True)
class FieldSpec:
    plan_name: str
    marvin_name: str
    to_marvin: ToMarvin = _identity
    normalize_live: NormalizeMarvin = _identity_live


FIELD_SPECS: dict[str, FieldSpec] = {
    "title": FieldSpec("title", "title"),
    "parent": FieldSpec("parent", "parentId", _parent_to_marvin, _parent_live),
    "scheduledDate": FieldSpec("scheduledDate", "day", _scheduled_to_marvin, _scheduled_live),
    "dueDate": FieldSpec("dueDate", "dueDate"),
    "startDate": FieldSpec("startDate", "startDate"),
    "endDate": FieldSpec("endDate", "endDate"),
    "plannedWeek": FieldSpec("plannedWeek", "plannedWeek"),
    "plannedMonth": FieldSpec("plannedMonth", "plannedMonth"),
    "labels": FieldSpec("labels", "labelIds", _labels_to_marvin, _labels_live),
    "estimatedTimeDuration": FieldSpec(
        "estimatedTimeDuration", "timeEstimate", _estimate_to_marvin
    ),
    "note": FieldSpec("note", "note"),
    "dayRank": FieldSpec("dayRank", "rank"),
    "masterRank": FieldSpec("masterRank", "masterRank"),
    "dailySection": FieldSpec("dailySection", "dailySection"),
    "bonusSection": FieldSpec("bonusSection", "bonusSection"),
    "customSectionId": FieldSpec("customSectionId", "customSection"),
    "timeBlockSectionId": FieldSpec("timeBlockSectionId", "timeBlockSection"),
    "starPriority": FieldSpec("starPriority", "isStarred", _priority_to_marvin),
    "frogSize": FieldSpec("frogSize", "isFrogged", _frog_to_marvin),
    "backburner": FieldSpec("backburner", "backburner"),
    "reviewDate": FieldSpec("reviewDate", "reviewDate"),
    "snoozedUntil": FieldSpec("snoozedUntil", "itemSnoozeTime", _snooze_to_marvin),
    "permanentSnoozeUntil": FieldSpec("permanentSnoozeUntil", "permaSnoozeTime"),
    "dependencies": FieldSpec(
        "dependencies", "dependsOn", _dependencies_to_marvin, _dependencies_live
    ),
}


def compile_plan_field(field: str, value: PlanValue) -> tuple[str, Any]:
    """Compile one validated plan field to its Marvin field/value pair."""

    spec = FIELD_SPECS[field]
    return spec.marvin_name, spec.to_marvin(value)


def live_field_matches(field: str, expected: PlanValue, document: dict[str, Any]) -> bool:
    """Compare a plan value with Marvin storage while ignoring review-only title hints."""

    spec = FIELD_SPECS[field]
    present = spec.marvin_name in document
    normalized = spec.normalize_live(document.get(spec.marvin_name), present)
    return normalized == spec.to_marvin(expected)


def field_snapshot(document: dict[str, Any], marvin_field: str) -> dict[str, Any]:
    """Preserve structural absence separately from an explicit JSON null."""

    if marvin_field in document:
        return {"present": True, "value": document[marvin_field]}
    return {"present": False}
