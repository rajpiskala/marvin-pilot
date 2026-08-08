"""Deterministic, credential-free plan descriptions."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from marvin_pilot.models.plan_v1 import ChangePlanV1, CreateOperation, UpdateOperation
from marvin_pilot.plan_io import plan_digest

FIELD_LABELS = {
    "title": "title",
    "parent": "parent",
    "scheduledDate": "scheduled date",
    "dueDate": "due date",
    "startDate": "start date",
    "endDate": "end date",
    "plannedWeek": "planned week",
    "plannedMonth": "planned month",
    "labels": "labels",
    "estimatedTimeDuration": "estimated time duration",
    "note": "note",
    "dayRank": "day rank",
    "masterRank": "master rank",
    "dailySection": "daily section",
    "bonusSection": "bonus section",
    "customSectionId": "custom section ID",
    "timeBlockSectionId": "time block section ID",
    "starPriority": "star priority",
    "frogSize": "frog size",
    "backburner": "backburner",
    "reviewDate": "review date",
    "snoozedUntil": "snoozed until",
    "permanentSnoozeUntil": "permanent snooze until",
    "dependencies": "dependencies",
}


def _format_ref(value: dict[str, Any]) -> str:
    title = value.get("title")
    return f"{title} [{value['id']}]" if title else f"[{value['id']}]"


def _format_value(field: str, value: Any) -> str:
    if value is None:
        return "none"
    if field == "parent":
        return _format_ref(value)
    if field == "labels":
        return ", ".join(_format_ref(item) for item in value) if value else "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    if isinstance(value, str) and field in {"title", "note"}:
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_plan_description(plan: ChangePlanV1) -> str:
    """Render a stable plain-text summary suitable for humans and LLMs."""

    lines = [
        f"Plan: {plan.summary}",
        f"Plan ID: {plan.planId}",
        f"Digest: {plan_digest(plan)}",
        "",
    ]
    counts: Counter[str] = Counter()

    for index, operation in enumerate(plan.operations, start=1):
        counts[operation.action] += 1
        title = getattr(operation.target, "title", None)
        display = f' "{title}"' if title is not None else ""
        lines.append(
            f"{index}. {operation.action.upper()}{display} ({operation.target.id}) "
            f"[{operation.operationId}]"
        )

        if isinstance(operation, UpdateOperation):
            before = operation.before.model_dump(exclude_unset=True, mode="json")
            after = operation.after.model_dump(exclude_unset=True, mode="json")
            for field, new_value in after.items():
                lines.append(
                    f"   {FIELD_LABELS[field]}: {_format_value(field, before[field])} -> "
                    f"{_format_value(field, new_value)}"
                )
        elif isinstance(operation, CreateOperation):
            after = operation.after.model_dump(exclude_unset=True, mode="json")
            for field, new_value in after.items():
                lines.append(f"   {FIELD_LABELS[field]}: {_format_value(field, new_value)}")

        expected_updated_at = getattr(operation, "expectedUpdatedAt", None)
        if expected_updated_at is not None:
            lines.append(f"   expected updatedAt: {expected_updated_at}")
        if operation.dependsOnOperations:
            lines.append(f"   depends on: {', '.join(operation.dependsOnOperations)}")
        lines.append(f"   reason: {operation.reason}")
        lines.append("")

    ordered_totals = [
        f"{counts[action]} {action}{'' if counts[action] == 1 else 's'}"
        for action in ("create", "update", "trash")
        if counts[action]
    ]
    lines.append("Totals: " + ", ".join(ordered_totals))
    return "\n".join(lines) + "\n"
