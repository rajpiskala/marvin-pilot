"""Receipt-driven, conflict-aware reversal of applied Marvin changes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from marvin_pilot.compiler import CompiledMutation
from marvin_pilot.errors import (
    AmbiguousMutationError,
    LivePreconditionError,
    MarvinPilotError,
    PartialMutationError,
    PlanSemanticError,
    RemoteError,
    UserDeclinedError,
)
from marvin_pilot.executor import (
    MAX_RECONCILED_MUTATION_RETRIES,
    MutationClient,
    compiled_mutation_matches,
    unix_milliseconds,
    verified_mutation_response,
)
from marvin_pilot.field_registry import field_snapshot
from marvin_pilot.history import HistoryStore, ReceiptHandle, rfc3339_utc
from marvin_pilot.models.receipt_v1 import ReceiptOperationV1, ReceiptV1, RequestRecord
from marvin_pilot.preflight import coupled_task_reasons


@dataclass(frozen=True, slots=True)
class RevertOperation:
    source_operation: ReceiptOperationV1
    live_document: dict[str, Any] | None
    compiled: CompiledMutation
    live_revision: dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class RevertPreflight:
    source_receipt: ReceiptV1
    source_receipt_path: Path
    operations: tuple[RevertOperation, ...]
    checked_at_ms: int
    strict_concurrency: bool


@dataclass(frozen=True, slots=True)
class RevertResult:
    receipt: ReceiptV1
    receipt_path: str


def _snapshot_matches(document: dict[str, Any], expected: dict[str, dict[str, Any]]) -> bool:
    return all(field_snapshot(document, field) == snapshot for field, snapshot in expected.items())


def _revision_snapshot(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {field: field_snapshot(document, field) for field in ("_rev", "updatedAt")}


def _restorable_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return a full document suitable for recreating a deleted CouchDB record."""

    restored = deepcopy(document)
    restored.pop("_rev", None)
    restored.pop("_deleted", None)
    return restored


def _absent_restore_value(field: str) -> Any:
    if field in {"day", "parentId", "firstScheduled"}:
        return "unassigned"
    if field == "labelIds":
        return []
    if field == "dependsOn":
        return {}
    if field == "subtasks":
        return {}
    if field == "subtaskList":
        return []
    if field == "title":
        raise LivePreconditionError("cannot safely restore a structurally absent task title")
    return None


def _snapshot_restore_value(field: str, snapshot: dict[str, Any]) -> Any:
    if snapshot["present"]:
        return snapshot.get("value")
    return _absent_restore_value(field)


def _update_payload(
    target_id: str,
    desired: dict[str, Any],
    *,
    now_ms: int,
) -> dict[str, Any]:
    setters: list[dict[str, Any]] = []
    for field, value in desired.items():
        setters.extend(
            [
                {"key": field, "val": value},
                {"key": f"fieldUpdates.{field}", "val": now_ms},
            ]
        )
    setters.append({"key": "updatedAt", "val": now_ms})
    return {"itemId": target_id, "setters": setters}


def _compile_inverse(
    source: ReceiptOperationV1,
    live: dict[str, Any] | None,
    *,
    now_ms: int,
) -> CompiledMutation:
    if source.action in {"update", "complete"}:
        assert live is not None
        desired = {
            field: _snapshot_restore_value(field, snapshot)
            for field, snapshot in source.beforeFields.items()
        }
        return CompiledMutation(
            operation_id=source.operationId,
            action=f"revert-{source.action}",
            target_id=source.targetId,
            endpoint="doc/update",
            payload=_update_payload(source.targetId, desired, now_ms=now_ms),
            desired_fields=desired,
            before_fields={field: field_snapshot(live, field) for field in desired},
        )
    if source.action == "create":
        assert live is not None
        return CompiledMutation(
            operation_id=source.operationId,
            action="revert-create",
            target_id=source.targetId,
            endpoint="doc/delete",
            payload={"itemId": source.targetId},
            desired_fields={},
            before_fields={},
        )
    if source.beforeDocument is None:
        raise PlanSemanticError(
            f"operation {source.operationId!r} was recorded before Pilot-managed deletion "
            "snapshots and cannot be safely restored"
        )
    restored = _restorable_document(source.beforeDocument)
    return CompiledMutation(
        operation_id=source.operationId,
        action="revert-trash",
        target_id=source.targetId,
        endpoint="doc/create",
        payload=restored,
        desired_fields={},
        before_fields={},
    )


def _snapshot_value(snapshot: dict[str, Any]) -> Any:
    return snapshot.get("value") if snapshot["present"] else "<absent>"


def _conflict_details(source: ReceiptOperationV1, document: dict[str, Any]) -> str:
    details = []
    for field, applied in source.afterFields.items():
        current = field_snapshot(document, field)
        if current == applied:
            continue
        original = source.beforeFields.get(field, {"present": False})
        details.append(
            f"{field}: original={_snapshot_value(original)!r}, "
            f"applied={_snapshot_value(applied)!r}, current={_snapshot_value(current)!r}"
        )
    return "; ".join(details)


def _selected_source_operations(
    source: ReceiptV1,
    only: Sequence[str],
) -> list[ReceiptOperationV1]:
    if source.kind != "apply":
        raise PlanSemanticError("revert requires an apply receipt, not a revert receipt")
    if source.status not in {"applied", "partial"}:
        raise PlanSemanticError(
            f"receipt status {source.status!r} is not safely revertible; inspect its journal"
        )
    requested = list(only)
    if len(requested) != len(set(requested)):
        raise PlanSemanticError("each --only operation ID may be provided at most once")
    by_id = {operation.operationId: operation for operation in source.operations}
    missing = [operation_id for operation_id in requested if operation_id not in by_id]
    if missing:
        raise PlanSemanticError("unknown operation ID(s): " + ", ".join(missing))
    candidates = (
        [by_id[operation_id] for operation_id in requested]
        if requested
        else [operation for operation in source.operations if operation.status == "applied"]
    )
    not_applied = [
        operation.operationId for operation in candidates if operation.status != "applied"
    ]
    if not_applied:
        raise PlanSemanticError(
            "only successfully applied operations can be reverted: " + ", ".join(not_applied)
        )
    if not candidates:
        raise PlanSemanticError("receipt contains no successfully applied operations to revert")
    return sorted(
        candidates,
        key=lambda operation: operation.applyIndex or 0,
        reverse=True,
    )


def preflight_revert(
    source: ReceiptV1,
    source_path: Path,
    only: Sequence[str],
    *,
    client: MutationClient,
    history: HistoryStore,
    now_ms: int,
    strict_concurrency: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> RevertPreflight:
    """Resolve a selection and verify every inverse before any write or approval."""

    if source.apiBaseHost != client.api_base_host:
        raise LivePreconditionError(
            f"receipt belongs to {source.apiBaseHost}, but the configured API is "
            f"{client.api_base_host}"
        )
    selected = _selected_source_operations(source, only)
    operation_ids = {operation.operationId for operation in selected}
    claims = history.find_revert_claims(
        source_apply_receipt_id=source.receiptId,
        operation_ids=operation_ids,
    )
    if claims:
        details = ", ".join(f"{key} ({claims[key]})" for key in sorted(claims))
        raise PlanSemanticError(f"operation(s) already reverted or claimed: {details}")

    checked: list[RevertOperation] = []
    total = len(selected)
    for index, source_operation in enumerate(selected, start=1):
        if progress is not None:
            progress(index - 1, total, source_operation.operationId)
        live = client.get_doc(source_operation.targetId)
        identity_document = live
        if source_operation.action == "trash":
            if source_operation.beforeDocument is None:
                raise PlanSemanticError(
                    f"operation {source_operation.operationId!r} was recorded before "
                    "Pilot-managed deletion snapshots and cannot be safely restored"
                )
            if live is not None:
                raise LivePreconditionError(
                    f"operation {source_operation.operationId!r} target is no longer deleted"
                )
            identity_document = source_operation.beforeDocument
        elif live is None:
            raise LivePreconditionError(
                f"operation {source_operation.operationId!r} target item no longer exists"
            )
        assert identity_document is not None
        expected_db = {
            "task": "Tasks",
            "project": "Categories",
            "recurringTask": "RecurringTasks",
        }[source_operation.targetType]
        if (
            identity_document.get("db") != expected_db
            or (
                source_operation.targetType == "project"
                and identity_document.get("type") != "project"
            )
            or (
                source_operation.targetType == "recurringTask"
                and identity_document.get("recurringType") != "task"
            )
        ):
            raise LivePreconditionError(
                f"operation {source_operation.operationId!r} target is not a live "
                f"{source_operation.targetType} document"
            )
        if source_operation.recurrence is not None:
            recurrence = source_operation.recurrence
            if (
                identity_document.get("recurring") is not True
                or identity_document.get("recurringTaskId") != recurrence.seriesId
                or identity_document.get("day") != recurrence.scheduledDate
            ):
                raise LivePreconditionError(
                    f"operation {source_operation.operationId!r} recurring occurrence identity "
                    "changed after apply"
                )
            template = client.get_doc(recurrence.seriesId)
            if (
                template is None
                or template.get("db") != "RecurringTasks"
                or template.get("recurringType") != "task"
            ):
                raise LivePreconditionError(
                    f"operation {source_operation.operationId!r} recurrence series identity "
                    "changed after apply"
                )
        if live is not None:
            reasons = coupled_task_reasons(
                live,
                allow_explicit_recurrence=(
                    source_operation.recurrence is not None
                    or source_operation.targetType == "recurringTask"
                ),
            )
            if reasons:
                raise LivePreconditionError(
                    f"operation {source_operation.operationId!r} is blocked by coupled "
                    "behavior: " + ", ".join(reasons)
                )
            if live.get("deletedAt") not in {None, ""}:
                raise LivePreconditionError(
                    f"operation {source_operation.operationId!r} target is already in Trash"
                )
            if source_operation.action == "create":
                if source_operation.afterDocument is None:
                    raise PlanSemanticError(
                        f"operation {source_operation.operationId!r} was recorded before exact "
                        "create snapshots and cannot be safely deleted"
                    )
                if live != source_operation.afterDocument:
                    raise LivePreconditionError(
                        f"operation {source_operation.operationId!r} cannot be reverted because "
                        "the created document changed after apply"
                    )
            elif not _snapshot_matches(live, source_operation.afterFields):
                raise LivePreconditionError(
                    f"operation {source_operation.operationId!r} cannot be reverted because "
                    f"applied field(s) changed: {_conflict_details(source_operation, live)}"
                )
        compiled = _compile_inverse(source_operation, live, now_ms=now_ms)
        checked.append(
            RevertOperation(
                source_operation=source_operation,
                live_document=live,
                compiled=compiled,
                live_revision=_revision_snapshot(live) if live is not None else {},
            )
        )
        if progress is not None:
            progress(index, total, source_operation.operationId)
    return RevertPreflight(
        source_receipt=source,
        source_receipt_path=source_path,
        operations=tuple(checked),
        checked_at_ms=now_ms,
        strict_concurrency=strict_concurrency,
    )


def recheck_revert_operation(
    checked: RevertOperation,
    client: MutationClient,
    *,
    strict_concurrency: bool,
) -> dict[str, Any] | None:
    current = client.get_doc(checked.compiled.target_id)
    if checked.source_operation.action == "trash":
        if current is not None:
            raise LivePreconditionError(
                f"operation {checked.source_operation.operationId!r} target was recreated "
                "after preflight"
            )
        return None
    if current is None:
        raise LivePreconditionError(
            f"operation {checked.source_operation.operationId!r} target disappeared after preflight"
        )
    if checked.source_operation.action == "create":
        if current != checked.source_operation.afterDocument:
            raise LivePreconditionError(
                f"operation {checked.source_operation.operationId!r} created document changed "
                "after preflight"
            )
    elif not _snapshot_matches(current, checked.source_operation.afterFields):
        raise LivePreconditionError(
            f"operation {checked.source_operation.operationId!r} applied fields changed after "
            f"preflight: {_conflict_details(checked.source_operation, current)}"
        )
    if strict_concurrency:
        current_revision = _revision_snapshot(current)
        for field, before in checked.live_revision.items():
            if before["present"] and current_revision[field] != before:
                raise LivePreconditionError(
                    f"operation {checked.source_operation.operationId!r} {field} changed after "
                    "preflight"
                )
    return current


def _receipt_operations(preflight: RevertPreflight) -> list[ReceiptOperationV1]:
    result = []
    for checked in preflight.operations:
        source = checked.source_operation
        if source.action in {"update", "complete"}:
            planned_before, planned_after = source.plannedAfter, source.plannedBefore
        elif source.action == "create":
            planned_before, planned_after = source.plannedAfter, None
        else:
            planned_before, planned_after = None, source.plannedBefore
        result.append(
            ReceiptOperationV1(
                operationId=source.operationId,
                action=source.action,
                targetId=source.targetId,
                targetType=source.targetType,
                targetTitle=(
                    checked.live_document.get("title")
                    if checked.live_document is not None
                    else source.targetTitle
                ),
                recurrence=(
                    source.recurrence.model_dump(mode="json")
                    if source.recurrence is not None
                    else None
                ),
                plannedBefore=planned_before,
                plannedAfter=planned_after,
                beforeFields=checked.compiled.before_fields,
                desiredFields=checked.compiled.desired_fields,
                request=RequestRecord(
                    endpoint=checked.compiled.endpoint,
                    payload=checked.compiled.payload,
                ),
            )
        )
    return result


def _send_inverse(client: MutationClient, compiled: CompiledMutation) -> Any:
    if compiled.endpoint == "doc/create":
        return client.create_doc(compiled.payload)
    if compiled.endpoint == "doc/delete":
        return client.delete_doc(compiled.target_id)
    return client.update_doc(compiled.target_id, compiled.payload["setters"])


def _reconcile_or_retry(
    client: MutationClient,
    checked: RevertOperation,
    *,
    strict_concurrency: bool,
) -> str:
    last_error: AmbiguousMutationError | None = None
    for attempt in range(MAX_RECONCILED_MUTATION_RETRIES):
        current = client.get_doc(checked.compiled.target_id)
        if compiled_mutation_matches(current, checked.compiled):
            return (
                "reverted-after-reconciliation"
                if attempt == 0
                else "reverted-after-reconciled-retry"
            )
        delay = getattr(client, "delay_before_reconciled_retry", None)
        if delay is not None:
            delay(attempt)
        try:
            recheck_revert_operation(checked, client, strict_concurrency=strict_concurrency)
        except LivePreconditionError as exc:
            raise AmbiguousMutationError(
                f"operation {checked.source_operation.operationId!r} has an ambiguous response "
                "and live state is mixed"
            ) from exc
        try:
            _send_inverse(client, checked.compiled)
        except AmbiguousMutationError as exc:
            last_error = exc
            continue
        return "reverted-after-safe-retry"
    current = client.get_doc(checked.compiled.target_id)
    if compiled_mutation_matches(current, checked.compiled):
        return "reverted-after-reconciled-retry"
    assert last_error is not None
    raise last_error


def _finish_failure(
    *,
    history: HistoryStore,
    handle: ReceiptHandle,
    operation: ReceiptOperationV1,
    error: BaseException,
    completed_count: int,
    unknown: bool,
    wall_clock: Callable[[], datetime],
) -> str:
    operation.status = (
        "unknown"
        if unknown
        else ("stale" if isinstance(error, LivePreconditionError) else "failed")
    )
    operation.error = str(error)
    operation.endedAt = rfc3339_utc(wall_clock())
    history.persist(handle)
    status = "partial-revert" if completed_count or unknown else "failed-revert"
    return str(history.finalize(handle, status))


def execute_revert(
    source: ReceiptV1,
    source_path: Path,
    only: Sequence[str],
    *,
    client: MutationClient,
    history: HistoryStore,
    approve: Callable[[RevertPreflight], bool],
    strict_concurrency: bool = True,
    now_ms: Callable[[], int] = unix_milliseconds,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    preflight_progress: Callable[[int, int, str], None] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> RevertResult:
    """Preflight, approve, journal, reverse-order revert, verify, and finalize."""

    history.ensure_writable()
    checked = preflight_revert(
        source,
        source_path,
        only,
        client=client,
        history=history,
        now_ms=now_ms(),
        strict_concurrency=strict_concurrency,
        progress=preflight_progress,
    )
    if not approve(checked):
        raise UserDeclinedError("revert declined; no Marvin changes were made")
    handle = history.begin_revert(
        source,
        source_path,
        _receipt_operations(checked),
        api_base_host=client.api_base_host,
    )
    handle.receipt.status = "reverting"
    history.persist(handle)
    total = len(checked.operations)
    completed_count = 0

    for index, checked_operation in enumerate(checked.operations):
        receipt_operation = handle.receipt.operations[index]
        if progress is not None:
            progress(completed_count, total, receipt_operation.operationId)
        receipt_operation.status = "checking"
        receipt_operation.startedAt = rfc3339_utc(wall_clock())
        history.persist(handle)
        sending_started = False
        try:
            recheck_revert_operation(
                checked_operation,
                client,
                strict_concurrency=strict_concurrency,
            )
            receipt_operation.status = "sending"
            history.persist(handle)
            sending_started = True
            outcome = "reverted"
            resulting_document: dict[str, Any] | None = None
            try:
                response = _send_inverse(client, checked_operation.compiled)
                resulting_document = verified_mutation_response(
                    response, checked_operation.compiled
                )
            except AmbiguousMutationError:
                outcome = _reconcile_or_retry(
                    client,
                    checked_operation,
                    strict_concurrency=strict_concurrency,
                )
            receipt_operation.status = "verifying"
            receipt_operation.outcome = outcome
            history.persist(handle)
            if resulting_document is None:
                resulting_document = client.get_doc(checked_operation.compiled.target_id)
            if not compiled_mutation_matches(resulting_document, checked_operation.compiled):
                raise RemoteError(
                    f"operation {receipt_operation.operationId!r} did not verify after revert"
                )
            if resulting_document is None:
                receipt_operation.afterFields = {}
            else:
                receipt_operation.afterFields = {
                    field: field_snapshot(resulting_document, field)
                    for field in checked_operation.compiled.desired_fields
                }
                if checked_operation.compiled.endpoint == "doc/create":
                    receipt_operation.afterDocument = deepcopy(resulting_document)
            receipt_operation.status = "reverted"
            receipt_operation.applyIndex = completed_count + 1
            receipt_operation.endedAt = rfc3339_utc(wall_clock())
            history.persist(handle)
            completed_count += 1
            if progress is not None:
                progress(completed_count, total, receipt_operation.operationId)
        except KeyboardInterrupt as exc:
            path = _finish_failure(
                history=history,
                handle=handle,
                operation=receipt_operation,
                error=exc,
                completed_count=completed_count,
                unknown=sending_started,
                wall_clock=wall_clock,
            )
            raise PartialMutationError(f"revert interrupted; inspect receipt {path}") from exc
        except MarvinPilotError as exc:
            path = _finish_failure(
                history=history,
                handle=handle,
                operation=receipt_operation,
                error=exc,
                completed_count=completed_count,
                unknown=sending_started,
                wall_clock=wall_clock,
            )
            if completed_count or sending_started:
                raise PartialMutationError(
                    f"revert stopped after {completed_count}/{total} operations; "
                    f"receipt {path}: {exc}"
                ) from exc
            if isinstance(exc, LivePreconditionError):
                raise LivePreconditionError(f"{exc}; receipt {path}") from exc
            raise RemoteError(f"{exc}; receipt {path}") from exc

    path = history.finalize(handle, "reverted")
    return RevertResult(receipt=handle.receipt, receipt_path=str(path))
