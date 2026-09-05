from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marvin_pilot.errors import (
    AmbiguousMutationError,
    LivePreconditionError,
    PartialMutationError,
    PlanSemanticError,
    RemoteError,
    UserDeclinedError,
)
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.executor import execute_apply
from marvin_pilot.history import HistoryStore
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.reverter import execute_revert, preflight_revert

APPLY_MS = 1_786_233_600_123
REVERT_MS = APPLY_MS + 60_000


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 8, 22, 15, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=10)
        return current


class InMemoryMarvin:
    api_base_host = "https://marvin.test"

    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = copy.deepcopy(documents)
        self.mutations: list[tuple[str, str]] = []
        self.before_send = None
        self.timeout_modes: dict[str, str] = {}
        self.attempts: dict[str, int] = {}

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        value = self.documents.get(item_id)
        return copy.deepcopy(value) if value is not None else None

    def _maybe_timeout(self, item_id: str, apply) -> None:
        self.attempts[item_id] = self.attempts.get(item_id, 0) + 1
        attempt = self.attempts[item_id]
        if self.before_send is not None:
            self.before_send(item_id)
        mode = self.timeout_modes.get(item_id)
        if mode == "apply-then-timeout" and attempt == 1:
            apply()
            raise AmbiguousMutationError("injected timeout")
        if mode == "timeout-then-succeed" and attempt == 1:
            raise AmbiguousMutationError("injected timeout")
        if mode == "always-timeout":
            raise AmbiguousMutationError("injected timeout")
        if mode == "success-without-write":
            return
        apply()

    def update_doc(self, item_id: str, setters: list[dict[str, Any]]) -> dict[str, bool]:
        def apply() -> None:
            document = self.documents[item_id]
            for setter in setters:
                key = setter["key"]
                if key.startswith("fieldUpdates."):
                    subkey = key.split(".", 1)[1]
                    document.setdefault("fieldUpdates", {})[subkey] = setter["val"]
                else:
                    document[key] = setter["val"]
            document["_rev"] = f"{self.attempts[item_id] + 1}-updated"
            self.mutations.append(("update", item_id))

        self._maybe_timeout(item_id, apply)
        return {"ok": True}

    def create_doc(self, document: dict[str, Any]) -> dict[str, bool]:
        item_id = document["_id"]

        def apply() -> None:
            created = copy.deepcopy(document)
            created["_rev"] = "1-created"
            self.documents[item_id] = created
            self.mutations.append(("create", item_id))

        self._maybe_timeout(item_id, apply)
        return {"ok": True}

    def delete_doc(self, item_id: str) -> dict[str, bool]:
        def apply() -> None:
            self.documents.pop(item_id)
            self.mutations.append(("delete", item_id))

        self._maybe_timeout(item_id, apply)
        return {"ok": True}


@pytest.fixture
def documents() -> dict[str, dict[str, Any]]:
    return {
        "task-wash-dishes-id": {
            "_id": "task-wash-dishes-id",
            "_rev": "1-wash",
            "db": "Tasks",
            "title": "Wash the dishes",
            "day": "2026-08-08",
            "firstScheduled": "2026-08-01",
            "updatedAt": 100,
        },
        "task-dinner-id": {
            "_id": "task-dinner-id",
            "_rev": "1-dinner",
            "db": "Tasks",
            "title": "Eat dinner with Jacob",
            "parentId": "unassigned",
            "updatedAt": 200,
        },
        "duplicate-task-id": {
            "_id": "duplicate-task-id",
            "_rev": "1-duplicate",
            "db": "Tasks",
            "title": "Study chapter 3",
            "updatedAt": 1_786_221_000_123,
        },
        "people-category-id": {
            "_id": "people-category-id",
            "db": "Categories",
            "title": "People",
        },
    }


def apply_fixture(tmp_path: Path, client: InMemoryMarvin, clock: Clock):
    raw = json.dumps(EXAMPLE_PLAN).encode()
    plan = parse_plan_bytes(raw)
    return execute_apply(
        plan,
        raw,
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=lambda _checked: True,
        now_ms=lambda: APPLY_MS,
        wall_clock=clock,
    )


def revert_fixture(
    tmp_path: Path,
    client: InMemoryMarvin,
    source_result,
    clock: Clock,
    *,
    only: list[str] | None = None,
    approve=lambda _checked: True,
):
    return execute_revert(
        source_result.receipt,
        Path(source_result.receipt_path),
        only or [],
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=approve,
        now_ms=lambda: REVERT_MS,
        wall_clock=clock,
    )


def test_project_completion_apply_and_revert_restores_open_state(tmp_path: Path) -> None:
    project_id = "project-completion-id"
    documents = {
        project_id: {
            "_id": project_id,
            "_rev": "1-project",
            "db": "Categories",
            "type": "project",
            "title": "Completed delivery",
            "done": False,
            "updatedAt": 100,
        }
    }
    value = {
        "schemaVersion": 1,
        "planId": "44444444-4444-4444-8444-444444444444",
        "createdAt": "2026-08-11T12:00:00-07:00",
        "summary": "Complete a project on its historical delivery date.",
        "operations": [
            {
                "operationId": "complete-delivery",
                "action": "complete",
                "target": {
                    "type": "project",
                    "id": project_id,
                    "title": "Completed delivery",
                },
                "reason": "The delivery finished on July 23.",
                "completedAt": "2026-07-23T18:30:00-07:00",
                "expectedUpdatedAt": 100,
            }
        ],
    }
    raw = json.dumps(value).encode()
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = execute_apply(
        parse_plan_bytes(raw),
        raw,
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=lambda _checked: True,
        now_ms=lambda: APPLY_MS,
        wall_clock=clock,
    )

    assert client.documents[project_id]["done"] is True
    assert client.documents[project_id]["doneDate"] == "2026-07-23"
    assert source.receipt.operations[0].targetType == "project"

    revert_fixture(tmp_path, client, source, clock)
    assert client.documents[project_id]["done"] is False
    assert client.documents[project_id]["doneDate"] is None


def test_ordered_same_document_chain_applies_and_reverts_as_one_plan(tmp_path: Path) -> None:
    task_id = "ordered-chain-task"
    original = {
        "_id": task_id,
        "_rev": "1-original",
        "db": "Tasks",
        "title": "Draft release",
        "day": "2026-08-07",
        "firstScheduled": "2026-08-07",
        "done": False,
        "updatedAt": 100,
    }
    value = {
        "schemaVersion": 1,
        "planId": "77777777-7777-4777-8777-777777777777",
        "createdAt": "2026-08-08T14:45:00-07:00",
        "summary": "Rename, reschedule, and complete a task in one ordered chain.",
        "operations": [
            {
                "operationId": "rename-release",
                "action": "update",
                "target": {"type": "task", "id": task_id, "title": "Draft release"},
                "reason": "Use the final outcome-oriented title.",
                "before": {"title": "Draft release"},
                "after": {"title": "Prepare release"},
                "expectedUpdatedAt": 100,
            },
            {
                "operationId": "finalize-release-title",
                "action": "update",
                "target": {"type": "task", "id": task_id, "title": "Prepare release"},
                "reason": "Advance the title through an explicit second state.",
                "dependsOnOperations": ["rename-release"],
                "before": {"title": "Prepare release"},
                "after": {"title": "Publish release"},
            },
            {
                "operationId": "reschedule-release",
                "action": "update",
                "target": {"type": "task", "id": task_id, "title": "Publish release"},
                "reason": "Record the actual delivery day.",
                "dependsOnOperations": ["finalize-release-title"],
                "before": {"scheduledDate": "2026-08-07"},
                "after": {"scheduledDate": "2026-08-08"},
            },
            {
                "operationId": "complete-release",
                "action": "complete",
                "target": {"type": "task", "id": task_id, "title": "Publish release"},
                "reason": "The renamed and rescheduled work is complete.",
                "dependsOnOperations": ["reschedule-release"],
                "completedAt": "2026-08-08T14:30:00-07:00",
            },
        ],
    }
    raw = json.dumps(value).encode()
    client = InMemoryMarvin({task_id: original})
    clock = Clock()

    source = execute_apply(
        parse_plan_bytes(raw),
        raw,
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=lambda checked: all(
            operation.operation.target.id == task_id for operation in checked.operations
        ),
        now_ms=lambda: APPLY_MS,
        wall_clock=clock,
    )

    assert client.documents[task_id]["title"] == "Publish release"
    assert client.documents[task_id]["day"] == "2026-08-08"
    assert client.documents[task_id]["done"] is True
    assert [operation.targetId for operation in source.receipt.operations] == [task_id] * 4
    assert source.receipt.operations[2].beforeFields["day"]["value"] == "2026-08-07"
    assert source.receipt.operations[3].beforeFields["done"] == {
        "present": True,
        "value": False,
    }
    completion_receipt = source.receipt.operations[3]
    assert completion_receipt.plannedBefore["completionDay"] == "2026-08-08"
    assert completion_receipt.plannedAfter["completionDay"] == "2026-08-08"
    assert completion_receipt.beforeDocument["day"] == "2026-08-08"
    assert completion_receipt.afterDocument["day"] == "2026-08-08"
    assert completion_receipt.completionHistory.documentDayVerified is True
    assert completion_receipt.completionHistory.serverHistoryStatus == "not-checked"

    reverted = revert_fixture(tmp_path, client, source, clock)

    assert [operation.operationId for operation in reverted.receipt.operations] == [
        "complete-release",
        "reschedule-release",
        "finalize-release-title",
        "rename-release",
    ]
    restored = client.documents[task_id]
    assert restored["title"] == original["title"]
    assert restored["day"] == original["day"]
    assert restored["done"] is False


def test_ordered_update_then_trash_restores_and_reverts_the_intermediate_state(
    tmp_path: Path,
) -> None:
    task_id = "rename-then-trash-task"
    original = {
        "_id": task_id,
        "_rev": "1-original",
        "db": "Tasks",
        "title": "Duplicate draft",
        "day": "unassigned",
        "updatedAt": 100,
    }
    value = {
        "schemaVersion": 1,
        "planId": "12121212-1212-4212-8212-121212121212",
        "createdAt": "2026-08-08T14:45:00-07:00",
        "summary": "Clarify a duplicate before deleting it.",
        "operations": [
            {
                "operationId": "rename-duplicate",
                "action": "update",
                "target": {"type": "task", "id": task_id, "title": "Duplicate draft"},
                "reason": "Make the reviewed deletion unambiguous.",
                "before": {"title": "Duplicate draft"},
                "after": {"title": "Duplicate release draft"},
                "expectedUpdatedAt": 100,
            },
            {
                "operationId": "trash-renamed-duplicate",
                "action": "trash",
                "target": {
                    "type": "task",
                    "id": task_id,
                    "title": "Duplicate release draft",
                },
                "reason": "Remove the confirmed duplicate.",
                "dependsOnOperations": ["rename-duplicate"],
            },
        ],
    }
    raw = json.dumps(value).encode()
    client = InMemoryMarvin({task_id: original})
    clock = Clock()

    source = execute_apply(
        parse_plan_bytes(raw),
        raw,
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=lambda _checked: True,
        now_ms=lambda: APPLY_MS,
        wall_clock=clock,
    )

    assert task_id not in client.documents
    assert source.receipt.operations[1].beforeDocument["title"] == "Duplicate release draft"

    revert_fixture(tmp_path, client, source, clock)

    assert client.documents[task_id]["title"] == original["title"]
    assert client.documents[task_id]["day"] == original["day"]


def test_completed_task_reparent_apply_and_revert_preserve_completion(tmp_path: Path) -> None:
    task_id = "completed-task-id"
    done_at = 1_784_856_600_000
    documents = {
        task_id: {
            "_id": task_id,
            "_rev": "1-task",
            "db": "Tasks",
            "title": "Prepare release notes",
            "parentId": "project-old",
            "done": True,
            "doneAt": done_at,
            "updatedAt": 100,
        },
        "project-old": {
            "_id": "project-old",
            "db": "Categories",
            "type": "project",
            "title": "Project Alpha",
            "parentId": "unassigned",
        },
        "project-new": {
            "_id": "project-new",
            "db": "Categories",
            "type": "project",
            "title": "Project Beta",
            "parentId": "unassigned",
        },
    }
    value = {
        "schemaVersion": 1,
        "planId": "1f7c28a6-6bbd-4629-93c3-73517834a169",
        "createdAt": "2026-08-18T12:00:00-07:00",
        "summary": "Reparent completed history without reopening it.",
        "operations": [
            {
                "operationId": "move-completed-task",
                "action": "update",
                "target": {"type": "task", "id": task_id, "title": "Prepare release notes"},
                "reason": "Group the completed work under its durable project.",
                "expectedUpdatedAt": 100,
                "before": {"parent": {"id": "project-old", "title": "Project Alpha"}},
                "after": {"parent": {"id": "project-new", "title": "Project Beta"}},
                "display": {
                    "beforePath": [
                        {"id": "project-old", "type": "project", "title": "Project Alpha"}
                    ],
                    "afterPath": [
                        {"id": "project-new", "type": "project", "title": "Project Beta"}
                    ],
                    "existingCompletedAt": "2026-07-23T18:30:00-07:00",
                },
            }
        ],
    }
    raw = json.dumps(value).encode()
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = execute_apply(
        parse_plan_bytes(raw),
        raw,
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=lambda _checked: True,
        now_ms=lambda: APPLY_MS,
        wall_clock=clock,
    )

    assert client.documents[task_id]["parentId"] == "project-new"
    assert client.documents[task_id]["done"] is True
    assert client.documents[task_id]["doneAt"] == done_at
    assert source.receipt.operations[0].beforeFields == {
        "parentId": {"present": True, "value": "project-old"}
    }

    revert_fixture(tmp_path, client, source, clock)
    assert client.documents[task_id]["parentId"] == "project-old"
    assert client.documents[task_id]["done"] is True
    assert client.documents[task_id]["doneAt"] == done_at


def test_subtask_consolidation_apply_and_revert_restores_exact_embedded_map(
    tmp_path: Path,
) -> None:
    parent_id = "parent-dinner"
    source_id = "loose-order-food"
    original_subtasks = {
        "existing-pickup": {
            "_id": "existing-pickup",
            "title": "Pick up food",
            "rank": 10,
            "done": False,
            "nativeExtension": {"keep": "exactly"},
        }
    }
    documents = {
        parent_id: {
            "_id": parent_id,
            "_rev": "1-parent",
            "db": "Tasks",
            "title": "Handle dinner",
            "subtasks": copy.deepcopy(original_subtasks),
            "updatedAt": 100,
        },
        source_id: {
            "_id": source_id,
            "_rev": "1-source",
            "db": "Tasks",
            "title": "Order food",
            "done": False,
            "day": "unassigned",
            "parentId": "unassigned",
            "timeEstimate": 600_000,
            "updatedAt": 200,
        },
    }
    value = {
        "schemaVersion": 1,
        "planId": "55555555-5555-4555-8555-555555555555",
        "createdAt": "2026-08-14T08:00:00-07:00",
        "summary": "Consolidate loose dinner work into ordered subtasks.",
        "operations": [
            {
                "operationId": "build-dinner-checklist",
                "action": "update",
                "target": {"type": "task", "id": parent_id, "title": "Handle dinner"},
                "reason": "Put the workflow in one ordered checklist.",
                "before": {
                    "subtasks": [{"id": "existing-pickup", "title": "Pick up food", "done": False}]
                },
                "after": {
                    "subtasks": [
                        {
                            "id": "converted-order",
                            "title": "Order food",
                            "done": False,
                            "sourceTask": {
                                "id": source_id,
                                "title": "Order food",
                                "acceptLoss": ["estimatedTimeDuration"],
                            },
                        },
                        {
                            "id": "existing-pickup",
                            "title": "Pick up the food",
                            "done": True,
                        },
                        {"id": "check-food", "title": "Check the food is correct"},
                    ]
                },
            },
            {
                "operationId": "trash-loose-order",
                "action": "trash",
                "target": {"type": "task", "id": source_id, "title": "Order food"},
                "reason": "The new subtask replaces this loose task.",
                "dependsOnOperations": ["build-dinner-checklist"],
            },
        ],
    }
    raw = json.dumps(value).encode()
    client = InMemoryMarvin(documents)
    clock = Clock()
    reviews = []
    source = execute_apply(
        parse_plan_bytes(raw),
        raw,
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=lambda checked: reviews.append(checked) or True,
        now_ms=lambda: APPLY_MS,
        wall_clock=clock,
    )

    applied = client.documents[parent_id]["subtasks"]
    assert list(applied) == ["converted-order", "existing-pickup", "check-food"]
    assert [item["rank"] for item in applied.values()] == [1, 2, 3]
    assert applied["existing-pickup"]["nativeExtension"] == {"keep": "exactly"}
    assert applied["existing-pickup"]["doneAt"] == APPLY_MS
    assert source_id not in client.documents
    assert reviews[0].warnings[0].check == "source-task-accepted-loss"
    assert source.receipt.operations[1].beforeDocument["timeEstimate"] == 600_000

    revert_fixture(tmp_path, client, source, clock)
    assert client.documents[parent_id]["subtasks"] == original_subtasks
    assert client.documents[source_id]["title"] == "Order food"
    assert client.documents[source_id]["timeEstimate"] == 600_000
    assert "deletedAt" not in client.documents[source_id]


def test_full_revert_runs_in_reverse_apply_order_and_restores_values(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.mutations.clear()

    result = revert_fixture(tmp_path, client, source, clock)

    assert result.receipt.status == "reverted"
    assert [operation.operationId for operation in result.receipt.operations] == [
        "trash-duplicate-math-task",
        "create-email-follow-up",
        "improve-dinner-task",
        "reschedule-wash-dishes",
    ]
    assert [item_id for _, item_id in client.mutations] == [
        "duplicate-task-id",
        "40d06376-9125-4e9e-a6bd-631cb0e6dc55",
        "task-dinner-id",
        "task-wash-dishes-id",
    ]
    assert client.documents["task-wash-dishes-id"]["day"] == "2026-08-08"
    assert client.documents["task-dinner-id"]["title"] == "Eat dinner with Jacob"
    assert client.documents["duplicate-task-id"]["title"] == "Study chapter 3"
    assert client.documents["duplicate-task-id"]["_rev"] == "1-created"
    assert "deletedAt" not in client.documents["duplicate-task-id"]
    assert "restoredAt" not in client.documents["duplicate-task-id"]
    assert "40d06376-9125-4e9e-a6bd-631cb0e6dc55" not in client.documents
    restored = result.receipt.operations[0]
    assert restored.request.endpoint == "doc/create"
    assert restored.request.payload["_id"] == "duplicate-task-id"
    assert "_rev" not in restored.request.payload


def test_revert_refuses_to_delete_a_created_document_that_was_edited(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    created_id = "40d06376-9125-4e9e-a6bd-631cb0e6dc55"
    client.documents[created_id]["note"] = "User edit after apply"
    client.documents[created_id]["_rev"] = "2-user-edit"
    client.mutations.clear()

    with pytest.raises(LivePreconditionError, match="created document changed"):
        revert_fixture(
            tmp_path,
            client,
            source,
            clock,
            only=["create-email-follow-up"],
        )

    assert created_id in client.documents
    assert client.mutations == []


def test_legacy_trash_receipt_without_document_snapshot_is_not_restored(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    source.receipt.operations[-1].beforeDocument = None

    with pytest.raises(PlanSemanticError, match="deletion snapshots"):
        revert_fixture(
            tmp_path,
            client,
            source,
            clock,
            only=["trash-duplicate-math-task"],
        )


@pytest.mark.parametrize(
    ("operation_id", "target_id", "expected_endpoint"),
    [
        ("trash-duplicate-math-task", "duplicate-task-id", "doc/create"),
        (
            "create-email-follow-up",
            "40d06376-9125-4e9e-a6bd-631cb0e6dc55",
            "doc/delete",
        ),
    ],
)
def test_delete_and_recreate_reverts_reconcile_ambiguous_success(
    tmp_path: Path,
    documents: dict,
    operation_id: str,
    target_id: str,
    expected_endpoint: str,
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.attempts.clear()
    client.timeout_modes[target_id] = "apply-then-timeout"

    result = revert_fixture(tmp_path, client, source, clock, only=[operation_id])

    operation = result.receipt.operations[0]
    assert operation.outcome == "reverted-after-reconciliation"
    assert operation.request.endpoint == expected_endpoint
    assert client.attempts[target_id] == 1


def test_multiple_only_ids_are_reverted_in_reverse_apply_order(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.mutations.clear()

    result = revert_fixture(
        tmp_path,
        client,
        source,
        clock,
        only=["reschedule-wash-dishes", "improve-dinner-task"],
    )

    assert result.receipt.selectedOperationIds == [
        "improve-dinner-task",
        "reschedule-wash-dishes",
    ]
    assert [item_id for _, item_id in client.mutations] == [
        "task-dinner-id",
        "task-wash-dishes-id",
    ]
    assert "duplicate-task-id" not in client.documents


def test_touched_field_conflict_fails_whole_preflight_before_any_write(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.mutations.clear()
    client.documents["task-dinner-id"]["title"] = "Edited after apply"

    with pytest.raises(LivePreconditionError, match=r"applied field.*changed") as error:
        revert_fixture(tmp_path, client, source, clock)
    assert "original='Eat dinner with Jacob'" in str(error.value)
    assert "applied='5:00pm Get deep dish pizza" in str(error.value)
    assert "current='Edited after apply'" in str(error.value)
    assert client.mutations == []
    assert not list(tmp_path.glob("pending-revert-*.json"))


def test_unrelated_field_edit_is_preserved(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.mutations.clear()
    client.documents["task-wash-dishes-id"]["note"] = "Keep this newer note"
    client.documents["task-wash-dishes-id"]["_rev"] = "3-unrelated-before-preflight"

    revert_fixture(
        tmp_path,
        client,
        source,
        clock,
        only=["reschedule-wash-dishes"],
    )
    assert client.documents["task-wash-dishes-id"]["note"] == "Keep this newer note"


def test_decline_writes_no_revert_receipt_or_mutation(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.mutations.clear()
    with pytest.raises(UserDeclinedError):
        revert_fixture(tmp_path, client, source, clock, approve=lambda _checked: False)
    assert client.mutations == []
    assert not list(tmp_path.glob("*revert-*.json"))


def test_unknown_duplicate_and_unapplied_selections_fail_before_writes(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.mutations.clear()
    source.receipt.operations[0].status = "not-started"
    for selection, message in [
        (["missing-op"], "unknown operation"),
        (["improve-dinner-task", "improve-dinner-task"], "at most once"),
        (["reschedule-wash-dishes"], "successfully applied"),
    ]:
        with pytest.raises(PlanSemanticError, match=message):
            revert_fixture(tmp_path, client, source, clock, only=selection)
    assert client.mutations == []


def test_second_revert_of_same_operation_is_refused(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    selection = ["reschedule-wash-dishes"]
    revert_fixture(tmp_path, client, source, clock, only=selection)
    client.mutations.clear()
    with pytest.raises(PlanSemanticError, match="already reverted or claimed"):
        revert_fixture(tmp_path, client, source, clock, only=selection)
    assert client.mutations == []


def test_partial_revert_can_resume_unknown_and_not_started_operations(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.mutations.clear()
    calls = 0

    def fail_second_send(_item_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            client.before_send = None
            raise RemoteError("injected unambiguous server response")

    client.before_send = fail_second_send
    with pytest.raises(PartialMutationError, match="stopped after 1/4"):
        revert_fixture(tmp_path, client, source, clock)

    partial = HistoryStore(tmp_path).load(next(tmp_path.glob("partial-revert-*.json")))
    assert [operation.status for operation in partial.operations] == [
        "reverted",
        "unknown",
        "not-started",
        "not-started",
    ]
    remaining = [
        operation.operationId for operation in partial.operations if operation.status != "reverted"
    ]

    resumed = revert_fixture(tmp_path, client, source, clock, only=remaining)

    assert resumed.receipt.status == "reverted"
    assert [operation.operationId for operation in resumed.receipt.operations] == remaining
    assert all(operation.status == "reverted" for operation in resumed.receipt.operations)


def test_recheck_after_approval_detects_stale_revision_and_journals_failure(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.mutations.clear()

    def approve(_checked) -> bool:
        client.documents["task-wash-dishes-id"]["_rev"] = "user-edit-after-review"
        return True

    with pytest.raises(LivePreconditionError, match="receipt"):
        revert_fixture(
            tmp_path,
            client,
            source,
            clock,
            only=["reschedule-wash-dishes"],
            approve=approve,
        )
    receipt = HistoryStore(tmp_path).load(next(tmp_path.glob("failed-revert-*.json")))
    assert receipt.operations[0].status == "stale"
    assert client.mutations == []


def test_pending_revert_receipt_exists_before_send(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)

    def before_send(_item_id: str) -> None:
        path = next(tmp_path.glob("pending-revert-*.json"))
        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["status"] == "reverting"
        assert value["operations"][0]["status"] == "sending"
        client.before_send = None

    client.before_send = before_send
    revert_fixture(
        tmp_path,
        client,
        source,
        clock,
        only=["reschedule-wash-dishes"],
    )


def test_ambiguous_timeout_is_reconciled_and_second_timeout_is_partial(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    target = "task-wash-dishes-id"
    client.attempts.clear()
    client.timeout_modes[target] = "apply-then-timeout"
    result = revert_fixture(tmp_path, client, source, clock, only=["reschedule-wash-dishes"])
    assert result.receipt.operations[0].outcome == "reverted-after-reconciliation"
    assert client.attempts[target] == 1

    second_client = InMemoryMarvin(documents)
    second_clock = Clock()
    second_source = apply_fixture(tmp_path / "second", second_client, second_clock)
    second_client.attempts.clear()
    second_client.timeout_modes[target] = "always-timeout"
    with pytest.raises(PartialMutationError, match="0/1"):
        revert_fixture(
            tmp_path / "second",
            second_client,
            second_source,
            second_clock,
            only=["reschedule-wash-dishes"],
        )
    receipt = HistoryStore(tmp_path / "second").load(
        next((tmp_path / "second").glob("partial-revert-*.json"))
    )
    assert receipt.operations[0].status == "unknown"


def test_wrong_api_host_and_revert_receipt_source_are_rejected(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    source.receipt.apiBaseHost = "https://another-environment.test"
    with pytest.raises(LivePreconditionError, match="belongs to"):
        revert_fixture(tmp_path, client, source, clock)

    source.receipt.apiBaseHost = client.api_base_host
    source.receipt.kind = "revert"
    with pytest.raises(PlanSemanticError, match="apply receipt"):
        preflight_revert(
            source.receipt,
            Path(source.receipt_path),
            [],
            client=client,
            history=HistoryStore(tmp_path),
            now_ms=REVERT_MS,
        )


def test_success_without_state_change_is_recorded_unknown(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    clock = Clock()
    source = apply_fixture(tmp_path, client, clock)
    client.timeout_modes["task-wash-dishes-id"] = "success-without-write"
    with pytest.raises(PartialMutationError, match="did not verify"):
        revert_fixture(
            tmp_path,
            client,
            source,
            clock,
            only=["reschedule-wash-dishes"],
        )
    receipt = HistoryStore(tmp_path).load(next(tmp_path.glob("partial-revert-*.json")))
    assert receipt.operations[0].status == "unknown"
