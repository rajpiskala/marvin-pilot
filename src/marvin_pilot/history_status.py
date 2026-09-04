"""Receipt-backed local status and read-only live state auditing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from marvin_pilot.errors import LivePreconditionError, PlanSemanticError
from marvin_pilot.field_registry import field_snapshot
from marvin_pilot.history import HistoryStore
from marvin_pilot.models.plan_v1 import ChangePlanV1
from marvin_pilot.models.receipt_v1 import ReceiptV1
from marvin_pilot.plan_io import plan_digest

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
        results.append(
            {
                "operationId": operation.operationId,
                "targetId": operation.targetId,
                "targetTitle": operation.targetTitle,
                "state": state,
                "detail": detail,
            }
        )
    return results
