"""Receipt-backed local status and read-only live state auditing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from marvin_pilot.completion import completion_local_date, usable_marvin_day
from marvin_pilot.errors import LivePreconditionError, PlanSemanticError
from marvin_pilot.field_registry import field_snapshot
from marvin_pilot.history import HistoryStore
from marvin_pilot.models.plan_v1 import ChangePlanV1
from marvin_pilot.models.receipt_v1 import ReceiptV1
from marvin_pilot.plan_io import parse_plan_bytes, plan_digest

LocalPlanStatus = Literal[
    "never-applied",
    "applied",
    "partial",
    "reverted",
    "selectively-reverted",
    "changed",
    "ambiguous",
]


@dataclass(frozen=True, slots=True)
class PlanHistoryStatus:
    status: LocalPlanStatus
    plan_id: str
    plan_digest: str
    receipt_path: str | None
    details: str


def plan_history_status(plan: ChangePlanV1, history: HistoryStore) -> PlanHistoryStatus:
    digest = plan_digest(plan)
    matches: list[tuple[Path, ReceiptV1]] = []
    for path in history.list_paths():
        receipt = history.load(path)
        if receipt.kind == "apply" and (
            receipt.planId == plan.planId or receipt.planDigest == digest
        ):
            matches.append((path, receipt))
    if not matches:
        return PlanHistoryStatus(
            "never-applied", plan.planId, digest, None, "no matching apply receipt"
        )
    path, receipt = matches[0]
    if receipt.status in {"pending", "applying"} or any(
        item.status in {"sending", "verifying", "unknown"} for item in receipt.operations
    ):
        status: LocalPlanStatus = "ambiguous"
    elif receipt.status in {"partial", "failed"}:
        status = "partial"
    elif receipt.status != "applied":
        status = "changed"
    else:
        applied = {item.operationId for item in receipt.operations if item.status == "applied"}
        claims = history.find_revert_claims(
            source_apply_receipt_id=receipt.receiptId,
            operation_ids=applied,
        )
        reverted = set(claims)
        if applied and reverted == applied:
            status = "reverted"
        elif reverted:
            status = "selectively-reverted"
        else:
            status = "applied"
    return PlanHistoryStatus(
        status,
        plan.planId,
        digest,
        str(path),
        f"apply receipt status={receipt.status}; {len(receipt.operations)} operation(s)",
    )


AuditState = Literal["matches", "changed-later", "missing", "present", "mismatch"]


def _timestamp_ms(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def audit_receipt_live(receipt: ReceiptV1, reader: Any) -> list[dict[str, Any]]:
    """Compare current full documents with a receipt's verified post-state."""

    if receipt.kind != "apply":
        raise PlanSemanticError("history audit requires an apply receipt")
    if receipt.accountUserId is not None:
        account = reader.check_connection()
        if account.account_user_id != receipt.accountUserId or (
            receipt.accountEmail is not None
            and account.account_email.casefold() != receipt.accountEmail.casefold()
        ):
            raise LivePreconditionError(
                f"receipt account mismatch: expected {receipt.accountEmail or 'unknown email'} "
                f"({receipt.accountUserId}), connected {account.account_email} "
                f"({account.account_user_id})"
            )
    applied_at = _timestamp_ms(receipt.endedAt)
    source_operations = {
        operation.get("operationId"): operation
        for operation in receipt.sourcePlan.get("operations", [])
        if isinstance(operation, dict) and isinstance(operation.get("operationId"), str)
    }
    results: list[dict[str, Any]] = []
    for operation in receipt.operations:
        if operation.status != "applied":
            results.append(
                {
                    "operationId": operation.operationId,
                    "targetId": operation.targetId,
                    "state": "mismatch",
                    "detail": f"receipt operation status is {operation.status}",
                }
            )
            continue
        document = reader.get_doc(operation.targetId)
        if operation.action == "trash":
            state: AuditState = "matches" if document is None else "present"
            detail = (
                "document remains absent" if document is None else "deleted document is present"
            )
        elif document is None:
            state = "missing"
            detail = "expected post-apply document is missing"
        else:
            exact = all(
                field_snapshot(document, field) == expected
                for field, expected in operation.afterFields.items()
            )
            if exact:
                state = "matches"
                detail = "reviewed post-state still matches"
            else:
                updated_at = document.get("updatedAt")
                changed_later = (
                    applied_at is not None
                    and isinstance(updated_at, (int, float))
                    and not isinstance(updated_at, bool)
                    and updated_at > applied_at
                )
                state = "changed-later" if changed_later else "mismatch"
                detail = (
                    "document changed after receipt completion"
                    if changed_later
                    else "current fields do not match the verified post-state"
                )
        result: dict[str, Any] = {
            "operationId": operation.operationId,
            "targetId": operation.targetId,
            "targetType": operation.targetType,
            "targetTitle": operation.targetTitle,
            "state": state,
            "detail": detail,
        }
        source_operation = source_operations.get(operation.operationId, {})
        if (
            operation.action == "complete"
            and operation.targetType == "task"
            and document is not None
        ):
            target = source_operation.get("target")
            source_recurrence = (
                target.get("recurrence")
                if isinstance(target, dict) and isinstance(target.get("recurrence"), dict)
                else None
            )
            planned_completed_at = source_operation.get("completedAt")
            if not isinstance(planned_completed_at, str):
                planned_completed_at = (operation.plannedAfter or {}).get("completedAt")
            expected_day = (
                completion_local_date(planned_completed_at)
                if isinstance(planned_completed_at, str)
                else None
            )
            actual_day = usable_marvin_day(document.get("day"))
            expected_done_at = _timestamp_ms(planned_completed_at)
            actual_done_at = document.get("doneAt")
            done_at_matches = (
                expected_done_at is not None
                and isinstance(actual_done_at, (int, float))
                and not isinstance(actual_done_at, bool)
                and int(actual_done_at) == expected_done_at
            )
            if expected_day is None:
                completion_state = "unknown"
            elif document.get("done") is not True:
                completion_state = "not-completed"
            elif not done_at_matches:
                completion_state = "completion-timestamp-changed"
            elif actual_day is None:
                completion_state = "missing-day"
            elif actual_day != expected_day:
                completion_state = "wrong-day"
            else:
                completion_state = "matches"
            repairable = completion_state in {"missing-day", "wrong-day"} and not (
                source_recurrence is not None and actual_day is None
            )
            result["completionHistory"] = {
                "state": completion_state,
                "expectedDay": expected_day,
                "actualDay": actual_day,
                "plannedCompletedAt": planned_completed_at,
                "doneAtMatches": done_at_matches,
                "discoverability": (
                    "document-day-ready" if completion_state == "matches" else "unverified"
                ),
                "repairable": repairable,
            }
            result["currentUpdatedAt"] = document.get("updatedAt")
            result["currentTitle"] = document.get("title")
            if source_recurrence is not None:
                result["recurrence"] = source_recurrence
            if completion_state != "matches":
                state = "mismatch" if repairable else state
                result["state"] = state
                result["detail"] = (
                    f"completion history {completion_state}: expected day "
                    f"{expected_day!r}, found {actual_day!r}; full document verified, "
                    "server /doneItems discoverability unverified"
                )
            else:
                result["detail"] = (
                    "completion document and history day match; server /doneItems "
                    "discoverability unverified"
                )
        results.append(result)
    return results


def build_completion_day_repair_plan(
    audit_rows: list[dict[str, Any]],
    *,
    expected_account: dict[str, str],
    created_at: datetime | None = None,
) -> ChangePlanV1:
    """Build a review-only repair plan for confirmed historical completion-day defects."""

    operations: list[dict[str, Any]] = []
    seen_targets: dict[str, str] = {}
    for row in audit_rows:
        completion = row.get("completionHistory")
        if not isinstance(completion, dict) or completion.get("repairable") is not True:
            continue
        target_id = row.get("targetId")
        title = row.get("currentTitle") or row.get("targetTitle")
        expected_day = completion.get("expectedDay")
        planned_completed_at = completion.get("plannedCompletedAt")
        if not all(isinstance(value, str) and value for value in (target_id, title, expected_day)):
            continue
        if not isinstance(planned_completed_at, str):
            continue
        prior_day = seen_targets.get(target_id)
        if prior_day is not None:
            if prior_day != expected_day:
                raise PlanSemanticError(
                    f"completion repair receipts disagree for task {target_id!r}: "
                    f"{prior_day!r} versus {expected_day!r}"
                )
            continue
        seen_targets[target_id] = expected_day
        target: dict[str, Any] = {"type": "task", "id": target_id, "title": title}
        recurrence = row.get("recurrence")
        if isinstance(recurrence, dict):
            current_occurrence_day = completion.get("actualDay")
            if not isinstance(current_occurrence_day, str):
                continue
            target["recurrence"] = recurrence | {"scheduledDate": current_occurrence_day}
        digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:16]
        operation: dict[str, Any] = {
            "operationId": f"repair-completion-day-{digest}",
            "action": "update",
            "target": target,
            "before": {"scheduledDate": completion.get("actualDay")},
            "after": {"scheduledDate": expected_day},
            "display": {"existingCompletedAt": planned_completed_at},
            "reason": (
                "Restore Marvin completed-item history to the local calendar date encoded "
                "by the original reviewed completedAt timestamp."
            ),
        }
        updated_at = row.get("currentUpdatedAt")
        if isinstance(updated_at, int) and not isinstance(updated_at, bool):
            operation["expectedUpdatedAt"] = updated_at
        operations.append(operation)
    if not operations:
        raise PlanSemanticError("receipt audit found no confirmed completion-day repairs")
    timestamp = (created_at or datetime.now(UTC)).astimezone(UTC)
    value = {
        "schemaVersion": 1,
        "planId": str(uuid4()),
        "createdAt": timestamp.isoformat().replace("+00:00", "Z"),
        "summary": f"Repair {len(operations)} historical completion-history day(s).",
        "expectedAccount": expected_account,
        "operations": operations,
    }
    return parse_plan_bytes(json.dumps(value, ensure_ascii=False).encode("utf-8"))
