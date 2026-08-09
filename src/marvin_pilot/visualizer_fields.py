"""Presentation metadata for every allowlisted Marvin Pilot task field."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class FieldPresentationSpec:
    """Human-facing treatment for one validated plan field."""

    label: str
    kind: str
    order: int


FIELD_PRESENTATIONS: dict[str, FieldPresentationSpec] = {
    "title": FieldPresentationSpec("Title", "title", 0),
    "parent": FieldPresentationSpec("Parent", "parent", 10),
    "labels": FieldPresentationSpec("Labels", "labels", 20),
    "estimatedTimeDuration": FieldPresentationSpec("Estimated time duration", "estimate", 30),
    "scheduledDate": FieldPresentationSpec("Scheduled date", "date", 40),
    "dueDate": FieldPresentationSpec("Due date", "date", 41),
    "startDate": FieldPresentationSpec("Start date", "date", 42),
    "endDate": FieldPresentationSpec("End date", "date", 43),
    "plannedWeek": FieldPresentationSpec("Planned week", "date", 44),
    "plannedMonth": FieldPresentationSpec("Planned month", "date", 45),
    "dailySection": FieldPresentationSpec("Daily section", "section", 50),
    "bonusSection": FieldPresentationSpec("Bonus section", "section", 51),
    "customSectionId": FieldPresentationSpec("Custom section", "section", 52),
    "timeBlockSectionId": FieldPresentationSpec("Time-block section", "section", 53),
    "starPriority": FieldPresentationSpec("Star priority", "indicator", 60),
    "frogSize": FieldPresentationSpec("Frog size", "indicator", 61),
    "backburner": FieldPresentationSpec("Backburner", "indicator", 62),
    "reviewDate": FieldPresentationSpec("Review date", "date", 63),
    "snoozedUntil": FieldPresentationSpec("Snoozed until", "datetime", 64),
    "permanentSnoozeUntil": FieldPresentationSpec("Permanent snooze time", "time", 65),
    "dependencies": FieldPresentationSpec("Dependencies", "dependencies", 70),
    "dayRank": FieldPresentationSpec("Day rank", "rank", 80),
    "masterRank": FieldPresentationSpec("Master rank", "rank", 81),
    "note": FieldPresentationSpec("Note", "note", 90),
}


def _humanize(field: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", field).replace("_", " ")
    return words[:1].upper() + words[1:]


def presentation_for(field: str) -> FieldPresentationSpec:
    """Return explicit field presentation metadata or a safe generic fallback."""

    return FIELD_PRESENTATIONS.get(field, FieldPresentationSpec(_humanize(field), "generic", 999))


def short_value(field: str, value: Any) -> str:
    """Format a validated value for compact task-card presentation."""

    if value is None:
        return "Cleared"
    if field == "parent":
        return value.get("title") or value["id"]
    if field == "labels":
        return ", ".join(item.get("title") or item["id"] for item in value) or "No labels"
    if field == "dependencies":
        count = len(value)
        return f"{count} dependenc{'y' if count == 1 else 'ies'}"
    if isinstance(value, bool):
        return "On" if value else "Off"
    if isinstance(value, list):
        return f"{len(value)} item{'s' if len(value) != 1 else ''}"
    if isinstance(value, dict):
        return value.get("title") or value.get("id") or "Object"
    return str(value)
