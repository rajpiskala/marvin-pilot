"""Journaled independent-target batches behind the experimental database flag."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass

from marvin_pilot.compiler import project_compiled_mutation
from marvin_pilot.database import MAX_DATABASE_WRITE_BYTES
from marvin_pilot.errors import AmbiguousMutationError, LivePreconditionError, PartialMutationError
from marvin_pilot.field_registry import field_snapshot
from marvin_pilot.history import rfc3339_utc
from marvin_pilot.models.plan_v1 import CompleteOperation, CreateOperation, UpdateOperation
from marvin_pilot.preflight import PreflightOperation, recheck_operation, revision_snapshot


@dataclass(frozen=True, slots=True)
class DatabaseBatch:
    operations: tuple[PreflightOperation, ...]
    boundary: str


_TASK_BATCH_EDIT_FIELDS = frozenset(
    {"title", "scheduledDate", "dueDate", "startDate", "endDate", "estimatedTimeDuration", "parent"}
)
_PROJECT_BATCH_EDIT_FIELDS = frozenset({"title", "scheduledDate"})


def database_eligible(checked):
    # Keep recurrence side effects on the existing API path until native parity is proven.
    return (
        checked.operation.target.type != "recurringTask"
        and getattr(checked.operation.target, "recurrence", None) is None
    )


def _plain_update(checked: PreflightOperation) -> bool:
    operation = checked.operation
    live = checked.live_document or {}
    if (
        not isinstance(operation, UpdateOperation)
        or operation.siblingOrder is not None
        or not database_eligible(checked)
        or live.get("recurringTaskId")
        or live.get("done") is True
        or live.get("archived") is True
    ):
        return False
    fields = operation.after.model_fields_set
    if operation.target.type == "task":
        return bool(fields) and fields <= _TASK_BATCH_EDIT_FIELDS
    if operation.target.type == "project":
        return bool(fields) and fields <= _PROJECT_BATCH_EDIT_FIELDS
    return False


def _parent_references(checked: PreflightOperation) -> set[str]:
    operation = checked.operation
    result: set[str] = set()
    current = (checked.live_document or {}).get("parentId")
    if isinstance(current, str) and current not in {"", "root", "unassigned"}:
        result.add(current)
    for side in (getattr(operation, "before", None), getattr(operation, "after", None)):
        parent = getattr(side, "parent", None)
        if parent is not None:
            result.add(parent.id)
    return result


def _edit_conflict(left: PreflightOperation, right: PreflightOperation) -> bool:
    a, b = left.operation, right.operation
    return (
        a.target.id == b.target.id
        or a.operationId in b.dependsOnOperations
        or b.operationId in a.dependsOnOperations
        or a.target.id in _parent_references(right)
        or b.target.id in _parent_references(left)
    )


def _bucket_edits(window: list[PreflightOperation]) -> list[PreflightOperation]:
    # Earliest legal batch wave for a bounded, plain-edit-only window. Keep the
    # input order among edits in the same wave, and preserve every edge to an
    # earlier target, explicit dependency, or parent reference.
    waves: list[int] = []
    for index, edit in enumerate(window):
        waves.append(
            max(
                (waves[other] + 1 for other in range(index) if _edit_conflict(window[other], edit)),
                default=0,
            )
        )
    return [window[index] for index in sorted(range(len(window)), key=waves.__getitem__)]


def _execution_order(operations, *, optimize: bool):
    window: list[PreflightOperation] = []
    for checked in operations:
        if optimize and _plain_update(checked):
            window.append(checked)
            if len(window) < 200:
                continue
            yield from _bucket_edits(window)
            window.clear()
            continue
        yield from _bucket_edits(window)
        window.clear()
        yield checked
    yield from _bucket_edits(window)


def _estimated_write_bytes(checked):
    current = checked.live_document
    if checked.compiled.endpoint == "doc/delete":
        image = {
            "_id": checked.operation.target.id,
            "_rev": (current or {}).get("_rev", ""),
            "_deleted": True,
            "db": (current or {}).get("db", "Tasks"),
            "D": 1,
        }
    else:
        image = project_compiled_mutation(current, checked.compiled)
    # Include the delimiter and reserve space for a runtime revision.
    return len(json.dumps(image, ensure_ascii=False).encode("utf-8")) + 64


def database_batch_schedule(
    operations,
    *,
    maximum=200,
    maximum_bytes=MAX_DATABASE_WRITE_BYTES,
    optimize=True,
):
    batch = []
    targets = set()
    operation_ids = set()
    references: set[str] = set()
    plain_only = True
    creates = 0
    size = 16
    reason = "start"
    for checked in _execution_order(operations, optimize=optimize):
        op = checked.operation
        plain = optimize and _plain_update(checked)
        eligible = database_eligible(checked)
        isolated = not eligible or (op.target.type in {"category", "project"} and not plain)
        parents = _parent_references(checked) if optimize else set()
        added_bytes = 0 if isolated else _estimated_write_bytes(checked)
        split = None
        if isolated:
            split = "api-compatibility" if not eligible else "container-operation"
        elif len(batch) >= maximum:
            split = "document-limit"
        elif size + added_bytes > maximum_bytes:
            split = "byte-limit"
        elif op.target.id in targets:
            split = "same-target"
        elif operation_ids.intersection(op.dependsOnOperations):
            split = "explicit-dependency"
        elif op.target.id in references or targets.intersection(parents):
            split = "parent-reference"
        elif (op.target.type == "project" and not plain_only) or (
            not plain and any(item.operation.target.type == "project" for item in batch)
        ):
            # Only combine a simple project edit with other proven plain edits.
            split = "project-edit-boundary"
        elif isinstance(op, CreateOperation) and creates:
            split = "create-limit"
        if batch and split:
            yield DatabaseBatch(tuple(batch), reason)
            batch, targets, operation_ids, references, creates = [], set(), set(), set(), 0
            plain_only = True
            size = 16
        if split:
            reason = split
        batch.append(checked)
        targets.add(op.target.id)
        operation_ids.add(op.operationId)
        references.update(parents)
        plain_only = plain_only and plain
        creates += isinstance(op, CreateOperation)
        size += added_bytes
        if isolated:
            yield DatabaseBatch(tuple(batch), reason)
            batch, targets, operation_ids, references, creates = [], set(), set(), set(), 0
            plain_only = True
            size = 16
            reason = "after-isolated-operation"
    if batch:
        yield DatabaseBatch(tuple(batch), reason)


def independent_batches(operations, **kwargs):
    for batch in database_batch_schedule(operations, **kwargs):
        yield batch.operations


def reconciled_database_match(document, expected, deleted_revision):
    """An uncertain write needs its exact full image and next revision, not a subset."""
    previous = expected.get("_rev")
    generation = int(previous.split("-", 1)[0]) + 1 if previous else 1
    revision = document.get("_rev") if document is not None else deleted_revision
    if not isinstance(revision, str) or not revision.startswith(f"{generation}-"):
        return False
    if expected.get("_deleted"):
        return document is None
    if document is None:
        return False

    def without_revision(doc):
        return {k: v for k, v in doc.items() if k != "_rev"}

    return without_revision(document) == without_revision(expected)


def execute_database_apply(checked_plan, handle, *, client, history, wall_clock, progress=None):
    from marvin_pilot.executor import ApplyResult, _send_mutation, compiled_mutation_matches

    completed = 0
    total = len(checked_plan.operations)
    receipts = {r.operationId: r for r in handle.receipt.operations}
    runtime_revisions = {}
    for batch in independent_batches(checked_plan.operations):
        sending = False
        current_receipt = receipts[batch[0].operation.operationId]
        try:
            client.prefetch_operations([checked.operation for checked in batch])
            documents = []
            for checked in batch:
                op = checked.operation
                current_receipt = receipts[op.operationId]
                current_receipt.status = "checking"
                current_receipt.startedAt = rfc3339_utc(wall_clock())
                expected_revision = runtime_revisions.get(op.target.id)
                if checked.prior_same_target_operation_id and expected_revision is None:
                    raise LivePreconditionError(
                        f"operation {op.operationId!r} is missing its prior runtime revision"
                    )
                current = recheck_operation(
                    checked,
                    client,
                    strict_concurrency=checked_plan.strict_concurrency,
                    expected_revision=expected_revision
                    if checked.prior_same_target_operation_id
                    else None,
                )
                current_receipt.beforeDocument = deepcopy(current)
                if database_eligible(checked):
                    documents.append(client.mutation_document(checked.compiled, current))
            if documents:
                client.database.validate_write_documents(documents)
            for checked in batch:
                receipts[checked.operation.operationId].status = "sending"
            history.persist(handle)
            sending = True
            outcomes = None
            ambiguous = False
            try:
                if database_eligible(batch[0]):
                    outcomes = client.database.write_documents(documents)
                else:
                    _send_mutation(client.api, batch[0].compiled)
            except AmbiguousMutationError:
                # Read every target before deciding which part of an uncertain batch committed.
                ambiguous = True
            client.clear_read_cache()
            after = client.get_documents([c.operation.target.id for c in batch])
            sent = {doc["_id"]: doc for doc in documents}
            failures = []
            for checked in batch:
                op = checked.operation
                receipt = receipts[op.operationId]
                document = after[op.target.id]
                row = outcomes.get(op.target.id) if outcomes is not None else None
                failure = None
                if row is not None and row.get("error"):
                    failure = "database rejected this item; refresh the plan before retrying"
                elif not compiled_mutation_matches(document, checked.compiled):
                    failure = "post-write state does not match this reviewed operation"
                elif (
                    row is not None and document is not None and document.get("_rev") != row["rev"]
                ):
                    failure = "document changed after the database write; review concurrent changes"
                elif (
                    row is not None
                    and document is None
                    and client.database.deleted_revisions.get(op.target.id) != row["rev"]
                ):
                    failure = "deletion revision could not be verified"
                elif (
                    ambiguous
                    and database_eligible(checked)
                    and not reconciled_database_match(
                        document,
                        sent[op.target.id],
                        client.database.deleted_revisions.get(op.target.id),
                    )
                ):
                    failure = "ambiguous write has a different image or revision; inspect recovery"
                if failure:
                    receipt.status = "failed" if row and row.get("error") else "unknown"
                    receipt.error = failure
                    receipt.endedAt = rfc3339_utc(wall_clock())
                    failures.append(op.operationId)
                    continue
                receipt.status = "applied"
                receipt.applyIndex = completed + 1
                receipt.endedAt = rfc3339_utc(wall_clock())
                receipt.outcome = (
                    "applied-database-reconciled"
                    if ambiguous
                    else "applied-database-batch"
                    if database_eligible(checked)
                    else "applied-api-compatibility"
                )
                receipt.afterFields = (
                    {
                        field: field_snapshot(document, field)
                        for field in checked.compiled.desired_fields
                    }
                    if document is not None
                    else {}
                )
                if checked.compiled.endpoint == "doc/create" or isinstance(op, CompleteOperation):
                    receipt.afterDocument = deepcopy(document)
                if receipt.completionHistory is not None:
                    receipt.completionHistory.documentDayVerified = True
                if document is not None:
                    runtime_revisions[op.target.id] = revision_snapshot(document)
                else:
                    runtime_revisions.pop(op.target.id, None)
                completed += 1
                if progress:
                    progress(completed, total, op.operationId)
            history.persist(handle)
            if failures:
                path = history.finalize(handle, "partial")
                raise PartialMutationError(
                    f"database batch had unsuccessful items {', '.join(failures)}; "
                    f"{completed}/{total} verified applied; receipt {path}"
                )
        except PartialMutationError:
            raise
        except (Exception, KeyboardInterrupt) as exc:
            for checked in batch:
                receipt = receipts[checked.operation.operationId]
                if receipt.status == "applied":
                    continue
                receipt.status = "unknown" if sending else "failed"
                receipt.error = (
                    str(exc)
                    if isinstance(exc, LivePreconditionError)
                    else "database batch stopped; inspect per-item state before retrying"
                )
                receipt.endedAt = rfc3339_utc(wall_clock())
            path = history.finalize(handle, "partial" if completed or sending else "failed")
            raise PartialMutationError(
                f"database apply stopped; {completed}/{total} verified applied; receipt {path}"
            ) from exc
    path = history.finalize(handle, "applied")
    return ApplyResult(receipt=handle.receipt, receipt_path=str(path))
