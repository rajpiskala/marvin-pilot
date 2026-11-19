"""Journaled independent-target batches behind the experimental database flag."""

from __future__ import annotations

from copy import deepcopy

from marvin_pilot.errors import AmbiguousMutationError, LivePreconditionError, PartialMutationError
from marvin_pilot.field_registry import field_snapshot
from marvin_pilot.history import rfc3339_utc
from marvin_pilot.models.plan_v1 import CompleteOperation, CreateOperation
from marvin_pilot.preflight import recheck_operation, revision_snapshot


def database_eligible(checked):
    # Keep recurrence side effects on the existing API path until native parity is proven.
    return (
        checked.operation.target.type != "recurringTask"
        and getattr(checked.operation.target, "recurrence", None) is None
    )


def independent_batches(operations, *, maximum=100):
    batch = []
    targets = set()
    operation_ids = set()
    creates = 0
    for checked in operations:
        op = checked.operation
        isolated = op.target.type in {"category", "project"} or not database_eligible(checked)
        if batch and (
            isolated
            or len(batch) >= maximum
            or op.target.id in targets
            or operation_ids.intersection(op.dependsOnOperations)
            or (isinstance(op, CreateOperation) and creates)
        ):
            yield batch
            batch, targets, operation_ids, creates = [], set(), set(), 0
        batch.append(checked)
        targets.add(op.target.id)
        operation_ids.add(op.operationId)
        creates += isinstance(op, CreateOperation)
        if isolated:
            yield batch
            batch, targets, operation_ids, creates = [], set(), set(), 0
    if batch:
        yield batch


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
