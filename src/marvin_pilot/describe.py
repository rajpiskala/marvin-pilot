"""Deterministic, credential-free plan descriptions."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from marvin_pilot.models.plan_v1 import (
    ChangePlanV1,
    CompleteOperation,
    CreateOperation,
    UpdateOperation,
)
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
    "subtasks": "subtasks",
    "cadence": "recurrence cadence",
    "dueInDays": "generated due in days",
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
    "orbit": "Orbit",
}


def _format_ref(value: dict[str, Any]) -> str:
    title = value.get("title")
    return f"{title} [{value['id']}]" if title else f"[{value['id']}]"


def format_plan_value(field: str, value: Any) -> str:
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


def _accepted_source_loss_lines(fields: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for subtask in fields.get("subtasks") or []:
        source = subtask.get("sourceTask") or {}
        accepted = source.get("acceptLoss") or []
        if not accepted:
            continue
        labels = ", ".join(FIELD_LABELS.get(field, field) for field in accepted)
        lines.append(
            f"   explicitly accepted sourceTask loss: {source.get('title', source['id'])} "
            f"[{source['id']}] -> {labels}"
        )
    return lines


def render_plan_description(plan: ChangePlanV1) -> str:
    """Render a stable plain-text summary suitable for humans and LLMs."""

    lines = [
        f"Plan: {plan.summary}",
        f"Plan ID: {plan.planId}",
        f"Digest: {plan_digest(plan)}",
    ]
    if plan.expectedAccount is not None:
        lines.append(
            f"Expected account: {plan.expectedAccount.email} "
            f"(user ID {plan.expectedAccount.userId})"
        )
    else:
        lines.append("Expected account: not pinned")
    lines.append("")
    counts: Counter[str] = Counter()

    for index, operation in enumerate(plan.operations, start=1):
        counts[operation.action] += 1
        title = getattr(operation.target, "title", None)
        display = f' "{title}"' if title is not None else ""
        lines.append(
            f"{index}. {operation.action.upper()}{display} "
            f"({operation.target.id}) "
            f"[{operation.operationId}]"
        )
        lines.append(f"   target type: {operation.target.type}")
        if operation.target.type == "recurringTask":
            lines.append("   recurrence scope: entire series")
        elif getattr(operation.target, "recurrence", None) is not None:
            recurrence = operation.target.recurrence
            lines.append("   recurrence scope: this occurrence only")
            lines.append(f"   series: {recurrence.seriesTitle} [{recurrence.seriesId}]")
            lines.append(f"   occurrence date: {recurrence.scheduledDate}")

        if isinstance(operation, UpdateOperation):
            before = operation.before.model_dump(exclude_unset=True, mode="json")
            after = operation.after.model_dump(exclude_unset=True, mode="json")
            for field, new_value in after.items():
                lines.append(
                    f"   {FIELD_LABELS[field]}: {format_plan_value(field, before[field])} -> "
                    f"{format_plan_value(field, new_value)}"
                )
            lines.extend(_accepted_source_loss_lines(after))
            if operation.siblingOrder is not None:
                order = operation.siblingOrder
                if order.beforeId is not None:
                    lines.append(f"   order: immediately before [{order.beforeId}]")
                elif order.afterId is not None:
                    lines.append(f"   order: immediately after [{order.afterId}]")
                else:
                    lines.append(f"   order: {order.position}")
        elif isinstance(operation, CreateOperation):
            after = operation.after.model_dump(exclude_unset=True, mode="json")
            for field, new_value in after.items():
                lines.append(f"   {FIELD_LABELS[field]}: {format_plan_value(field, new_value)}")
            lines.extend(_accepted_source_loss_lines(after))
        elif isinstance(operation, CompleteOperation):
            lines.append(f"   completed at: {operation.completedAt}")

        expected_updated_at = getattr(operation, "expectedUpdatedAt", None)
        if expected_updated_at is not None:
            lines.append(f"   expected updatedAt: {expected_updated_at}")
        if operation.dependsOnOperations:
            lines.append(f"   depends on: {', '.join(operation.dependsOnOperations)}")
        lines.append(f"   reason: {operation.reason}")
        lines.append("")

    ordered_totals = [
        f"{counts[action]} {action}{'' if counts[action] == 1 else 's'}"
        for action in ("create", "update", "complete", "trash")
        if counts[action]
    ]
    lines.append("Totals: " + ", ".join(ordered_totals))
    return "\n".join(lines) + "\n"


def plan_description_payload(plan: ChangePlanV1) -> dict[str, Any]:
    """Return a stable, machine-readable description without live data."""

    counts = Counter(operation.action for operation in plan.operations)
    operations: list[dict[str, Any]] = []
    for index, operation in enumerate(plan.operations, start=1):
        value: dict[str, Any] = {
            "index": index,
            "operationId": operation.operationId,
            "action": operation.action,
            "target": operation.target.model_dump(mode="json", exclude_none=True),
            "reason": operation.reason,
            "dependsOnOperations": operation.dependsOnOperations,
        }
        if isinstance(operation, UpdateOperation):
            value["before"] = operation.before.model_dump(exclude_unset=True, mode="json")
            value["after"] = operation.after.model_dump(exclude_unset=True, mode="json")
            if operation.siblingOrder is not None:
                value["siblingOrder"] = operation.siblingOrder.model_dump(
                    mode="json", exclude_none=True
                )
        elif isinstance(operation, CreateOperation):
            value["after"] = operation.after.model_dump(exclude_unset=True, mode="json")
        elif isinstance(operation, CompleteOperation):
            value["completedAt"] = operation.completedAt
        if operation.display is not None:
            value["display"] = operation.display.model_dump(
                mode="json", exclude_none=True, exclude_unset=True
            )
        expected_updated_at = getattr(operation, "expectedUpdatedAt", None)
        if expected_updated_at is not None:
            value["expectedUpdatedAt"] = expected_updated_at
        operations.append(value)
    return {
        "kind": "plan",
        "schemaVersion": plan.schemaVersion,
        "planId": plan.planId,
        "summary": plan.summary,
        "createdAt": plan.createdAt,
        "digest": plan_digest(plan),
        "expectedAccount": (
            plan.expectedAccount.model_dump(mode="json")
            if plan.expectedAccount is not None
            else None
        ),
        "counts": {
            action: counts[action]
            for action in ("create", "update", "complete", "trash")
            if counts[action]
        },
        "operations": operations,
    }


def render_plan_markdown(plan: ChangePlanV1) -> str:
    """Render a compact Markdown review artifact suitable for sharing privately."""

    payload = plan_description_payload(plan)
    account = payload["expectedAccount"]
    account_text = (
        f"{account['email']} (`{account['userId']}`)" if account is not None else "Not pinned"
    )
    totals = ", ".join(f"{count} {action}" for action, count in payload["counts"].items())
    lines = [
        f"# {plan.summary}",
        "",
        f"- Plan ID: `{plan.planId}`",
        f"- Digest: `{payload['digest']}`",
        f"- Created: {plan.createdAt}",
        f"- Expected account: {account_text}",
        f"- Changes: {totals}",
        "",
        "## Operations",
        "",
    ]
    for operation in payload["operations"]:
        target = operation["target"]
        title = target.get("title") or target["id"]
        lines.extend(
            [
                f"### {operation['index']}. {operation['action'].title()}: {title}",
                "",
                f"- Target: `{target['type']}:{target['id']}`",
                f"- Operation ID: `{operation['operationId']}`",
                f"- Reason: {operation['reason']}",
            ]
        )
        if operation["dependsOnOperations"]:
            dependencies = ", ".join(f"`{item}`" for item in operation["dependsOnOperations"])
            lines.append(f"- Depends on: {dependencies}")
        if "completedAt" in operation:
            lines.append(f"- Completed at: {operation['completedAt']}")
        if "siblingOrder" in operation:
            lines.append(
                "- Relative order: `"
                + json.dumps(operation["siblingOrder"], ensure_ascii=False, sort_keys=True)
                + "`"
            )
        for field in ("before", "after", "display"):
            if field in operation:
                lines.extend(
                    [
                        f"- {field.title()}:",
                        "",
                        "```json",
                        json.dumps(operation[field], ensure_ascii=False, indent=2, sort_keys=True),
                        "```",
                    ]
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_live_preflight(result: PreflightResult) -> str:
    """Add live-state and compiler-managed changes to the offline reviewed diff."""

    rendered = render_plan_description(result.plan).rstrip()
    lines = [
        rendered,
        "",
        f"Live preflight: PASSED for {len(result.operations)} operation(s)",
        f"Strict concurrency recheck: {'on' if result.strict_concurrency else 'off'}",
        (
            f"Reads: {result.unique_documents_checked} unique document(s), "
            f"{result.metadata_collections_checked} metadata collection(s); "
            f"elapsed {result.elapsed_ms / 1000:.1f}s"
        ),
    ]
    if result.warnings:
        lines.append(f"Warnings: {len(result.warnings)}")
        lines.extend(
            f"  {warning.operation_index}. [{warning.operation_id}] {warning.message}"
            for warning in result.warnings
        )
    for checked in result.operations:
        operation = checked.operation
        desired = checked.compiled.desired_fields
        compiler_fields: list[str] = []
        if "firstScheduled" in desired:
            compiler_fields.append(f"firstScheduled={desired['firstScheduled']}")
        if operation.action == "create":
            compiler_fields.append(f"{operation.target.type} identity/defaults/timestamps")
        if operation.action == "complete":
            compiler_fields.append("done state and historical completion timestamp")
        if operation.action == "trash":
            compiler_fields.append(
                "API deletion with a full Pilot recovery snapshot journaled before send"
            )
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
        title = (
            (checked.live_document.get("title") if checked.live_document is not None else None)
            or source.targetTitle
            or source.targetId
        )
        lines.append(
            f'{index}. REVERT {source.action.upper()} "{title}" '
            f"({source.targetId}) [{source.operationId}]"
        )
        if source.action == "create":
            lines.append(f"   delete the {source.targetType} created by this operation")
        elif source.action == "trash":
            lines.append(f"   recreate the {source.targetType} from the Pilot recovery snapshot")
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
