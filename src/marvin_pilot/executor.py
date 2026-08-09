"""Journal-first apply state machine with strict rechecks and response reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

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
from marvin_pilot.field_registry import field_snapshot
from marvin_pilot.history import HistoryStore, ReceiptHandle, rfc3339_utc
from marvin_pilot.models.plan_v1 import ChangePlanV1
from marvin_pilot.models.receipt_v1 import ReceiptOperationV1, ReceiptV1
from marvin_pilot.plan_io import plan_digest
from marvin_pilot.preflight import (
    PreflightOperation,
    PreflightResult,
    desired_fields_match,
    preflight_plan,
    recheck_operation,
)

MAX_RECONCILED_MUTATION_RETRIES = 3


class MutationClient(Protocol):
    api_base_host: str

    def get_doc(self, item_id: str) -> dict[str, Any] | None: ...

    def update_doc(self, item_id: str, setters: list[dict[str, Any]]) -> Any: ...

    def create_doc(self, document: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class ApplyResult:
    receipt: ReceiptV1
    receipt_path: str


def unix_milliseconds() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000)


def _send_mutation(client: MutationClient, compiled: CompiledMutation) -> None:
    if compiled.action == "create":
        client.create_doc(compiled.payload)
    else:
        client.update_doc(compiled.target_id, compiled.payload["setters"])


def _reconcile_or_retry(
    client: MutationClient,
    checked: PreflightOperation,
    *,
    strict_concurrency: bool,
) -> str:
    """Resolve an ambiguous mutation response without blindly repeating its POST."""

    last_error: AmbiguousMutationError | None = None
    for attempt in range(MAX_RECONCILED_MUTATION_RETRIES):
        current = client.get_doc(checked.compiled.target_id)
        if desired_fields_match(current, checked.compiled.desired_fields):
            return (
                "applied-after-reconciliation"
                if attempt == 0
                else "applied-after-reconciled-retry"
            )
        delay = getattr(client, "delay_before_reconciled_retry", None)
        if delay is not None:
            delay(attempt)
        try:
            recheck_operation(checked, client, strict_concurrency=strict_concurrency)
        except LivePreconditionError as exc:
            raise AmbiguousMutationError(
                f"operation {checked.operation.operationId!r} has an ambiguous response "
                "and live state is mixed"
            ) from exc
        try:
            _send_mutation(client, checked.compiled)
        except AmbiguousMutationError as exc:
            last_error = exc
            continue
        return "applied-after-safe-retry"
    current = client.get_doc(checked.compiled.target_id)
    if desired_fields_match(current, checked.compiled.desired_fields):
        return "applied-after-reconciled-retry"
    assert last_error is not None
    raise last_error


def _finish_failure(
    *,
    history: HistoryStore,
    handle: ReceiptHandle,
    receipt_operation: ReceiptOperationV1,
    error: BaseException,
    completed_count: int,
    unknown: bool,
    wall_clock: Callable[[], datetime],
) -> str:
    receipt_operation.status = (
        "unknown"
        if unknown
        else ("stale" if isinstance(error, LivePreconditionError) else "failed")
    )
    receipt_operation.error = str(error)
    receipt_operation.endedAt = rfc3339_utc(wall_clock())
    history.persist(handle)
    terminal_status = "partial" if completed_count or unknown else "failed"
    return str(history.finalize(handle, terminal_status))


def execute_apply(
    plan: ChangePlanV1,
    source_plan_bytes: bytes,
    *,
    client: MutationClient,
    history: HistoryStore,
    approve: Callable[[PreflightResult], bool],
    strict_concurrency: bool = True,
    now_ms: Callable[[], int] = unix_milliseconds,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: Callable[[int, int, str], None] | None = None,
) -> ApplyResult:
    """Preflight, approve, journal, apply, verify, and finalize one complete plan."""

    history.ensure_writable()
    digest = plan_digest(plan)
    duplicate = history.find_duplicate(plan_id=plan.planId, digest=digest)
    if duplicate is not None:
        path, receipt = duplicate
        raise PlanSemanticError(
            f"plan was already recorded as {receipt.status!r} in receipt {path}"
        )

    checked_plan = preflight_plan(
        plan,
        client,
        now_ms=now_ms(),
        strict_concurrency=strict_concurrency,
    )
    if not approve(checked_plan):
        raise UserDeclinedError("apply declined; no Marvin changes were made")

    handle = history.begin_apply(
        checked_plan,
        source_plan_bytes,
        api_base_host=client.api_base_host,
    )
    handle.receipt.status = "applying"
    history.persist(handle)
    completed_count = 0
    total = len(checked_plan.operations)

    for index, checked in enumerate(checked_plan.operations):
        receipt_operation = handle.receipt.operations[index]
        receipt_operation.status = "checking"
        receipt_operation.startedAt = rfc3339_utc(wall_clock())
        history.persist(handle)
        sending_started = False
        try:
            recheck_operation(checked, client, strict_concurrency=strict_concurrency)
            receipt_operation.status = "sending"
            history.persist(handle)
            sending_started = True
            outcome = "applied"
            try:
                _send_mutation(client, checked.compiled)
            except AmbiguousMutationError:
                outcome = _reconcile_or_retry(
                    client,
                    checked,
                    strict_concurrency=strict_concurrency,
                )

            receipt_operation.status = "verifying"
            receipt_operation.outcome = outcome
            history.persist(handle)
            resulting_document = client.get_doc(checked.compiled.target_id)
            if not desired_fields_match(resulting_document, checked.compiled.desired_fields):
                raise RemoteError(
                    f"operation {checked.operation.operationId!r} did not verify after mutation"
                )
            assert resulting_document is not None
            receipt_operation.afterFields = {
                field: field_snapshot(resulting_document, field)
                for field in checked.compiled.desired_fields
            }
            receipt_operation.status = "applied"
            receipt_operation.applyIndex = completed_count + 1
            receipt_operation.endedAt = rfc3339_utc(wall_clock())
            history.persist(handle)
            completed_count += 1
            if progress is not None:
                progress(completed_count, total, checked.operation.operationId)
        except KeyboardInterrupt as exc:
            path = _finish_failure(
                history=history,
                handle=handle,
                receipt_operation=receipt_operation,
                error=exc,
                completed_count=completed_count,
                unknown=sending_started,
                wall_clock=wall_clock,
            )
            raise PartialMutationError(f"apply interrupted; inspect receipt {path}") from exc
        except MarvinPilotError as exc:
            unknown = sending_started
            path = _finish_failure(
                history=history,
                handle=handle,
                receipt_operation=receipt_operation,
                error=exc,
                completed_count=completed_count,
                unknown=unknown,
                wall_clock=wall_clock,
            )
            if completed_count or unknown:
                raise PartialMutationError(
                    f"apply stopped after {completed_count}/{total} operations; "
                    f"receipt {path}: {exc}"
                ) from exc
            if isinstance(exc, LivePreconditionError):
                raise LivePreconditionError(f"{exc}; receipt {path}") from exc
            raise RemoteError(f"{exc}; receipt {path}") from exc

    path = history.finalize(handle, "applied")
    return ApplyResult(receipt=handle.receipt, receipt_path=str(path))
