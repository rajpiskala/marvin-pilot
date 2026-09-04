"""The sole allowlisted mapping between plan fields and Marvin task fields."""

from __future__ import annotations

import math
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


def _indicator_live(value: Any, present: bool) -> int | None:
    if not present or value in {None, False}:
        return None
    return value


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


def _subtasks_to_marvin(value: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for rank, item in enumerate(value, start=1):
        result[item["id"]] = {
            "_id": item["id"],
            "title": item["title"],
            "rank": rank,
            "done": item.get("done", False),
        }
    return result


def _invalid_subtasks() -> dict[str, dict[str, Any]]:
    return {"<invalid>": {"_id": "<invalid>", "title": "<invalid>", "rank": 1, "done": False}}


def _subtasks_live(value: Any, present: bool) -> dict[str, dict[str, Any]]:
    if not present or value is None:
        return {}
    if not isinstance(value, dict):
        return _invalid_subtasks()
    ordered: list[tuple[float, int, dict[str, Any]]] = []
    seen_ranks: set[float] = set()
    for position, (key, raw) in enumerate(value.items()):
        if not isinstance(key, str) or not isinstance(raw, dict):
            return _invalid_subtasks()
        identifier = raw.get("_id", key)
        title = raw.get("title")
        if not isinstance(identifier, str) or identifier != key or not isinstance(title, str):
            return _invalid_subtasks()
        rank = raw.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, (int, float)) or not math.isfinite(rank):
            return _invalid_subtasks()
        sortable_rank = float(rank)
        if sortable_rank in seen_ranks:
            return _invalid_subtasks()
        seen_ranks.add(sortable_rank)
        done = raw.get("done", False)
        if not isinstance(done, bool):
            return _invalid_subtasks()
        ordered.append(
            (
                sortable_rank,
                position,
                {"id": identifier, "title": title, "done": done},
            )
        )
    ordered.sort(key=lambda entry: (entry[0], entry[1]))
    return _subtasks_to_marvin([entry[2] for entry in ordered])


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
    "subtasks": FieldSpec("subtasks", "subtasks", _subtasks_to_marvin, _subtasks_live),
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
    "orbit": FieldSpec("orbit", "orbit"),
}


def _recurring_subtasks_to_marvin(value: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    if value is None:
        return []
    return [{"_id": item["id"], "title": item["title"]} for item in value]


def _recurring_subtasks_live(value: Any, present: bool) -> list[dict[str, str]]:
    if not present or value is None:
        return []
    if not isinstance(value, list):
        return [{"_id": "<invalid>", "title": "<invalid>"}]
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            return [{"_id": "<invalid>", "title": "<invalid>"}]
        identifier = item.get("_id")
        title = item.get("title")
        if not isinstance(identifier, str) or not isinstance(title, str):
            return [{"_id": "<invalid>", "title": "<invalid>"}]
        normalized.append({"_id": identifier, "title": title})
    return normalized


def _cadence_to_marvin(value: dict[str, Any]) -> dict[str, Any]:
    native: dict[str, Any] = {
        "type": value["type"],
        "repeatStart": value["startDate"],
        "endDate": value.get("endDate"),
    }
    cadence_type = value["type"]
    if cadence_type == "weekly":
        native["day"] = value["weekday"]
    elif cadence_type == "monthly":
        native["date"] = value["monthDate"]
        native["limitToWeekdays"] = value.get("limitToWeekdays", False)
    elif cadence_type == "n per week":
        native["weekDays"] = value["weekdays"]
    elif cadence_type in {"repeat", "repeat week", "repeat month", "repeat year"}:
        native["repeat"] = value["interval"]
        if cadence_type == "repeat month":
            native["limitToWeekdays"] = value.get("limitToWeekdays", False)
    elif cadence_type == "echo":
        native["echoDays"] = value["daysAfterCompletion"]
    elif cadence_type == "onOff":
        native["onCount"] = value["onDays"]
        native["offCount"] = value["offDays"]
    elif cadence_type == "custom":
        native["customRecurrence"] = value["expression"]
    return native


@dataclass(frozen=True, slots=True)
class RecurringFieldSpec:
    plan_name: str
    marvin_name: str | None = None
    to_marvin: ToMarvin = _identity
    normalize_live: NormalizeMarvin = _identity_live


RECURRING_TASK_FIELD_SPECS: dict[str, RecurringFieldSpec] = {
    "title": RecurringFieldSpec("title", "title"),
    "parent": RecurringFieldSpec("parent", "parentId", _parent_to_marvin, _parent_live),
    "labels": RecurringFieldSpec("labels", "labelIds", _labels_to_marvin, _labels_live),
    "estimatedTimeDuration": RecurringFieldSpec(
        "estimatedTimeDuration", "timeEstimate", _estimate_to_marvin
    ),
    "note": RecurringFieldSpec("note", "note"),
    "subtasks": RecurringFieldSpec(
        "subtasks", "subtaskList", _recurring_subtasks_to_marvin, _recurring_subtasks_live
    ),
    "starPriority": RecurringFieldSpec(
        "starPriority", "isStarred", _priority_to_marvin, _indicator_live
    ),
    "frogSize": RecurringFieldSpec("frogSize", "isFrogged", _frog_to_marvin, _indicator_live),
    "dueInDays": RecurringFieldSpec("dueInDays", "dueIn"),
    "cadence": RecurringFieldSpec("cadence"),
}


def compile_recurring_task_field(field: str, value: PlanValue) -> dict[str, Any]:
    """Compile one recurring-series field to one or more native template fields."""

    spec = RECURRING_TASK_FIELD_SPECS[field]
    if field == "cadence":
        return _cadence_to_marvin(value)
    assert spec.marvin_name is not None
    return {spec.marvin_name: spec.to_marvin(value)}


def recurring_task_field_matches(field: str, expected: PlanValue, document: dict[str, Any]) -> bool:
    """Compare a recurring-series plan value with its native template representation."""

    spec = RECURRING_TASK_FIELD_SPECS[field]
    if field == "cadence":
        return all(
            document.get(key) == value for key, value in _cadence_to_marvin(expected).items()
        )
    assert spec.marvin_name is not None
    present = spec.marvin_name in document
    normalized = spec.normalize_live(document.get(spec.marvin_name), present)
    return normalized == spec.to_marvin(expected)


def compile_fields_for_target(target_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Compile plan fields into their semantic native values for comparisons and writes."""

    if target_type == "recurringTask":
        result: dict[str, Any] = {}
        for field, value in fields.items():
            result.update(compile_recurring_task_field(field, value))
        return result
    return dict(compile_plan_field(field, value) for field, value in fields.items())


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
