"""Deterministic, credential-free plan descriptions."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from marvin_pilot.models.plan_v1 import ChangePlanV1, CreateOperation, UpdateOperation
from marvin_pilot.plan_io import plan_digest
from marvin_pilot.preflight import PreflightResult
from marvin_pilot.reverter import RevertPreflight

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


def render_live_preflight(result: PreflightResult) -> str:
    """Add live-state and compiler-managed changes to the offline reviewed diff."""

    rendered = render_plan_description(result.plan).rstrip()
    lines = [
        rendered,
        "",
        f"Live preflight: PASSED for {len(result.operations)} operation(s)",
        f"Strict concurrency recheck: {'on' if result.strict_concurrency else 'off'}",
    ]
    for checked in result.operations:
        operation = checked.operation
        desired = checked.compiled.desired_fields
        compiler_fields: list[str] = []
        if "firstScheduled" in desired:
            compiler_fields.append(f"firstScheduled={desired['firstScheduled']}")
        if operation.action == "create":
            compiler_fields.append("task identity/defaults/timestamps")
        if operation.action == "trash":
            compiler_fields.append("deletedAt and field update timestamps")
        if compiler_fields:
            lines.append(
                f"Compiler-managed [{operation.operationId}]: {', '.join(compiler_fields)}"
            )
    return "\n".join(lines) + "\n"


def render_revert_preflight(result: RevertPreflight) -> str:
    """Render the selected receipt inverses in their actual reverse execution order."""

    lines = [
        f"Revert apply receipt: {result.source_receipt.receiptId}",
        f"Source: {result.source_receipt_path}",
        f"Plan: {result.source_receipt.sourcePlan.get('summary', result.source_receipt.planId)}",
        f"Selected operations: {len(result.operations)}",
        "Execution order: reverse apply order",
        "",
    ]
    for index, checked in enumerate(result.operations, start=1):
        source = checked.source_operation
        title = checked.live_document.get("title") or source.targetTitle or source.targetId
        lines.append(
            f'{index}. REVERT {source.action.upper()} "{title}" '
            f"({source.targetId}) [{source.operationId}]"
        )
        if source.action == "create":
            lines.append("   move the task created by this operation to Marvin Trash")
        elif source.action == "trash":
            lines.append("   restore the task from Marvin Trash")
        for field, desired in checked.compiled.desired_fields.items():
            before = checked.compiled.before_fields[field]
            old_value = before.get("value") if before["present"] else "<absent>"
            lines.append(f"   Marvin {field}: {old_value!r} -> {desired!r}")
        lines.append("")
    lines.extend(
        [
            f"Live revert preflight: PASSED for {len(result.operations)} operation(s)",
            f"Strict concurrency recheck: {'on' if result.strict_concurrency else 'off'}",
        ]
    )
    return "\n".join(lines) + "\n"
