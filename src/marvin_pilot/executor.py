"""Journal-first apply state machine with strict rechecks and response reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
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
    revision_snapshot,
)

MAX_RECONCILED_MUTATION_RETRIES = 3


class MutationClient(Protocol):
    api_base_host: str

    def get_doc(self, item_id: str) -> dict[str, Any] | None: ...

    def update_doc(self, item_id: str, setters: list[dict[str, Any]]) -> Any: ...

    def create_doc(self, document: dict[str, Any]) -> Any: ...

    def delete_doc(self, item_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class ApplyResult:
    receipt: ReceiptV1
    receipt_path: str


def unix_milliseconds() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000)


def _send_mutation(client: MutationClient, compiled: CompiledMutation) -> Any:
    if compiled.endpoint == "doc/create":
        return client.create_doc(compiled.payload)
    if compiled.endpoint == "doc/delete":
        return client.delete_doc(compiled.target_id)
    return client.update_doc(compiled.target_id, compiled.payload["setters"])


def _nested_value_matches(document: dict[str, Any], path: str, expected: Any) -> bool:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return current == expected


def compiled_mutation_matches(document: dict[str, Any] | None, compiled: CompiledMutation) -> bool:
    """Verify every reviewed storage value, including nested metadata setters."""

    if compiled.endpoint == "doc/delete":
        return document is None
    if document is None or not desired_fields_match(document, compiled.desired_fields):
        return False
    if compiled.endpoint == "doc/create":
        return all(
            _nested_value_matches(document, field, value)
            for field, value in compiled.payload.items()
        )
    return all(
        _nested_value_matches(document, setter["key"], setter["val"])
        for setter in compiled.payload["setters"]
    )


def verified_mutation_response(response: Any, compiled: CompiledMutation) -> dict[str, Any] | None:
    """Use Marvin's returned document only when it proves the reviewed state was stored."""

    if (
        not isinstance(response, dict)
        or response.get("_id") != compiled.target_id
        or not compiled_mutation_matches(response, compiled)
    ):
        return None
    return response


def _reconcile_or_retry(
    client: MutationClient,
    checked: PreflightOperation,
    *,
    strict_concurrency: bool,
    expected_revision: dict[str, Any] | None = None,
) -> str:
    """Resolve an ambiguous mutation response without blindly repeating its POST."""

    last_error: AmbiguousMutationError | None = None
    for attempt in range(MAX_RECONCILED_MUTATION_RETRIES):
        current = client.get_doc(checked.compiled.target_id)
        if compiled_mutation_matches(current, checked.compiled):
            return (
                "applied-after-reconciliation" if attempt == 0 else "applied-after-reconciled-retry"
            )
        delay = getattr(client, "delay_before_reconciled_retry", None)
        if delay is not None:
            delay(attempt)
        try:
            recheck_operation(
                checked,
                client,
                strict_concurrency=strict_concurrency,
                expected_revision=expected_revision,
            )
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
    if compiled_mutation_matches(current, checked.compiled):
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
    preflight_progress: Callable[[int, int, str], None] | None = None,
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
        progress=preflight_progress,
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
    runtime_revisions: dict[str, dict[str, Any]] = {}

    for index, checked in enumerate(checked_plan.operations):
        receipt_operation = handle.receipt.operations[index]
        if progress is not None:
            progress(completed_count, total, checked.operation.operationId)
        receipt_operation.status = "checking"
        receipt_operation.startedAt = rfc3339_utc(wall_clock())
        history.persist(handle)
        sending_started = False
        try:
            expected_revision = (
                runtime_revisions.get(checked.operation.target.id)
                if checked.prior_same_target_operation_id is not None
                else None
            )
            if checked.prior_same_target_operation_id is not None and expected_revision is None:
                raise LivePreconditionError(
                    f"operation {checked.operation.operationId!r} is missing the runtime "
                    f"revision produced by prior same-target operation "
                    f"{checked.prior_same_target_operation_id!r}"
                )
            current_document = recheck_operation(
                checked,
                client,
                strict_concurrency=strict_concurrency,
                expected_revision=expected_revision,
            )
            if checked.compiled.endpoint == "doc/delete":
                assert current_document is not None
                receipt_operation.beforeDocument = deepcopy(current_document)
            receipt_operation.status = "sending"
            history.persist(handle)
            sending_started = True
            outcome = "applied"
            resulting_document: dict[str, Any] | None = None
            try:
                response = _send_mutation(client, checked.compiled)
                resulting_document = verified_mutation_response(response, checked.compiled)
            except AmbiguousMutationError:
                outcome = _reconcile_or_retry(
                    client,
                    checked,
                    strict_concurrency=strict_concurrency,
                    expected_revision=expected_revision,
                )

            receipt_operation.status = "verifying"
            receipt_operation.outcome = outcome
            history.persist(handle)
            if resulting_document is None:
                resulting_document = client.get_doc(checked.compiled.target_id)
            if not compiled_mutation_matches(resulting_document, checked.compiled):
                raise RemoteError(
                    f"operation {checked.operation.operationId!r} did not verify after mutation"
                )
            if resulting_document is None:
                receipt_operation.afterFields = {}
            else:
                receipt_operation.afterFields = {
                    field: field_snapshot(resulting_document, field)
                    for field in checked.compiled.desired_fields
                }
                if checked.compiled.endpoint == "doc/create":
                    receipt_operation.afterDocument = deepcopy(resulting_document)
                runtime_revisions[checked.operation.target.id] = revision_snapshot(
                    resulting_document
                )
            if resulting_document is None:
                runtime_revisions.pop(checked.operation.target.id, None)
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
