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

NOW_MS = 1_786_233_600_123


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
        self.reads: list[str] = []
        self.mutations: list[tuple[str, str]] = []
        self.fail_target: str | None = None
        self.timeout_modes: dict[str, str] = {}
        self.attempts: dict[str, int] = {}
        self.before_send = None
        self.return_documents = False
        self.ignore_field_update_setters = False

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        self.reads.append(item_id)
        value = self.documents.get(item_id)
        return copy.deepcopy(value) if value is not None else None

    def _maybe_fail(self, item_id: str, apply) -> None:
        self.attempts[item_id] = self.attempts.get(item_id, 0) + 1
        attempt = self.attempts[item_id]
        if self.before_send is not None:
            self.before_send(item_id)
        if item_id == self.fail_target:
            raise RemoteError(f"injected failure for {item_id}")
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

    def update_doc(self, item_id: str, setters: list[dict[str, Any]]) -> dict[str, Any]:
        def apply() -> None:
            document = self.documents[item_id]
            for setter in setters:
                key = setter["key"]
                if key.startswith("fieldUpdates."):
                    if self.ignore_field_update_setters:
                        continue
                    subkey = key.split(".", 1)[1]
                    document.setdefault("fieldUpdates", {})[subkey] = setter["val"]
                else:
                    document[key] = setter["val"]
            document["_rev"] = f"{self.attempts[item_id] + 1}-updated"
            self.mutations.append(("update", item_id))

        self._maybe_fail(item_id, apply)
        if self.return_documents:
            return copy.deepcopy(self.documents[item_id])
        return {"ok": True}

    def create_doc(self, document: dict[str, Any]) -> dict[str, Any]:
        item_id = document["_id"]

        def apply() -> None:
            created = copy.deepcopy(document)
            created["_rev"] = "1-created"
            self.documents[item_id] = created
            self.mutations.append(("create", item_id))

        self._maybe_fail(item_id, apply)
        if self.return_documents:
            return copy.deepcopy(self.documents[item_id])
        return {"ok": True}

    def delete_doc(self, item_id: str) -> dict[str, bool]:
        def apply() -> None:
            self.documents.pop(item_id)
            self.mutations.append(("delete", item_id))

        self._maybe_fail(item_id, apply)
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


def plan_and_raw():
    raw = json.dumps(EXAMPLE_PLAN, indent=2).encode()
    return parse_plan_bytes(raw), raw


def trash_plan_and_raw():
    value = copy.deepcopy(EXAMPLE_PLAN)
    value["operations"] = [value["operations"][-1]]
    raw = json.dumps(value, indent=2).encode()
    return parse_plan_bytes(raw), raw


def run_apply(
    tmp_path: Path,
    client: InMemoryMarvin,
    *,
    approve=lambda _preflight: True,
    progress=None,
):
    plan, raw = plan_and_raw()
    clock = Clock()
    return execute_apply(
        plan,
        raw,
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=approve,
        now_ms=lambda: NOW_MS,
        wall_clock=clock,
        progress=progress,
    )


def run_trash_apply(tmp_path: Path, client: InMemoryMarvin):
    plan, raw = trash_plan_and_raw()
    clock = Clock()
    return execute_apply(
        plan,
        raw,
        client=client,
        history=HistoryStore(tmp_path, now=clock),
        approve=lambda _preflight: True,
        now_ms=lambda: NOW_MS,
        wall_clock=clock,
    )


def test_apply_is_preflighted_journaled_verified_and_completed(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    approval_calls = []
    progress = []

    def approve(preflight) -> bool:
        approval_calls.append(preflight)
        assert client.mutations == []
        assert len(preflight.operations) == 4
        return True

    result = run_apply(
        tmp_path,
        client,
        approve=approve,
        progress=lambda current, total, operation_id: progress.append(
            (current, total, operation_id)
        ),
    )
    assert len(approval_calls) == 1
    assert result.receipt.status == "applied"
    assert Path(result.receipt_path).name.startswith("applied-")
    assert [operation.status for operation in result.receipt.operations] == ["applied"] * 4
    assert [operation.applyIndex for operation in result.receipt.operations] == [1, 2, 3, 4]
    assert client.documents["task-wash-dishes-id"]["day"] == "2026-08-09"
    assert client.documents["task-dinner-id"]["timeEstimate"] == 16_200_000
    assert "duplicate-task-id" not in client.documents
    assert "40d06376-9125-4e9e-a6bd-631cb0e6dc55" in client.documents
    trash_receipt = result.receipt.operations[-1]
    assert trash_receipt.request.endpoint == "doc/delete"
    assert trash_receipt.beforeDocument["title"] == "Study chapter 3"
    assert trash_receipt.afterDocument is None
    assert progress[0] == (0, 4, "reschedule-wash-dishes")
    assert progress[-1] == (4, 4, "trash-duplicate-math-task")


def test_verified_marvin_response_skips_the_redundant_readback(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    client.return_documents = True

    run_apply(tmp_path, client)

    for target_id in (
        "task-wash-dishes-id",
        "task-dinner-id",
        "40d06376-9125-4e9e-a6bd-631cb0e6dc55",
    ):
        assert client.reads.count(target_id) == 2
    assert client.reads.count("duplicate-task-id") == 3


def test_apply_rejects_a_write_that_omits_nested_field_update_metadata(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    client.return_documents = True
    client.ignore_field_update_setters = True

    with pytest.raises(PartialMutationError, match="did not verify"):
        run_apply(tmp_path, client)

    receipt = HistoryStore(tmp_path).load(next(tmp_path.glob("partial-*.json")))
    assert receipt.operations[0].status == "unknown"


def test_pending_receipt_is_durable_before_first_send(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)

    def before_send(_item_id: str) -> None:
        paths = list(tmp_path.glob("pending-*.json"))
        assert len(paths) == 1
        value = json.loads(paths[0].read_text(encoding="utf-8"))
        assert value["status"] == "applying"
        assert value["operations"][0]["status"] == "sending"
        client.before_send = None

    client.before_send = before_send
    run_apply(tmp_path, client)


def test_trash_snapshot_is_durable_and_current_immediately_before_delete(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)

    def before_send(item_id: str) -> None:
        assert item_id == "duplicate-task-id"
        path = next(tmp_path.glob("pending-*.json"))
        value = json.loads(path.read_text(encoding="utf-8"))
        operation = value["operations"][0]
        assert operation["status"] == "sending"
        assert operation["request"] == {
            "endpoint": "doc/delete",
            "payload": {"itemId": "duplicate-task-id"},
        }
        assert operation["beforeDocument"] == documents["duplicate-task-id"]
        client.before_send = None

    client.before_send = before_send
    run_trash_apply(tmp_path, client)
    assert "duplicate-task-id" not in client.documents


@pytest.mark.parametrize(
    ("mode", "expected_outcome", "expected_attempts"),
    [
        ("apply-then-timeout", "applied-after-reconciliation", 1),
        ("timeout-then-succeed", "applied-after-safe-retry", 2),
    ],
)
def test_trash_reconciles_ambiguous_delete_without_duplicate_loss(
    tmp_path: Path,
    documents: dict,
    mode: str,
    expected_outcome: str,
    expected_attempts: int,
) -> None:
    client = InMemoryMarvin(documents)
    client.timeout_modes["duplicate-task-id"] = mode

    result = run_trash_apply(tmp_path, client)

    assert result.receipt.operations[0].outcome == expected_outcome
    assert client.attempts["duplicate-task-id"] == expected_attempts
    assert "duplicate-task-id" not in client.documents


def test_decline_makes_no_receipt_and_no_writes(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    with pytest.raises(UserDeclinedError):
        run_apply(tmp_path, client, approve=lambda _preflight: False)
    assert client.mutations == []
    assert list(tmp_path.glob("*.json")) == []


def test_duplicate_successful_apply_is_refused(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    run_apply(tmp_path, client)
    with pytest.raises(PlanSemanticError, match="already recorded"):
        run_apply(tmp_path, client)


def test_stale_recheck_fails_before_first_write_with_receipt(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)

    def approve(_preflight) -> bool:
        client.documents["task-wash-dishes-id"]["_rev"] = "2-user-edit"
        return True

    with pytest.raises(LivePreconditionError, match="receipt"):
        run_apply(tmp_path, client, approve=approve)
    assert client.mutations == []
    receipt_path = next(tmp_path.glob("failed-*.json"))
    receipt = HistoryStore(tmp_path).load(receipt_path)
    assert receipt.operations[0].status == "stale"


def test_failure_after_one_write_stops_with_partial_receipt(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    client.fail_target = "task-dinner-id"
    with pytest.raises(PartialMutationError, match="1/4"):
        run_apply(tmp_path, client)
    assert client.mutations == [("update", "task-wash-dishes-id")]
    receipt = HistoryStore(tmp_path).load(next(tmp_path.glob("partial-*.json")))
    assert receipt.operations[0].status == "applied"
    assert receipt.operations[1].status == "unknown"
    assert all(operation.status == "not-started" for operation in receipt.operations[2:])


def test_timeout_that_applied_is_reconciled_without_retry(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    client.timeout_modes["task-wash-dishes-id"] = "apply-then-timeout"
    result = run_apply(tmp_path, client)
    first = result.receipt.operations[0]
    assert first.outcome == "applied-after-reconciliation"
    assert client.attempts["task-wash-dishes-id"] == 1


def test_timeout_with_unchanged_state_gets_one_safe_retry(tmp_path: Path, documents: dict) -> None:
    client = InMemoryMarvin(documents)
    client.timeout_modes["task-wash-dishes-id"] = "timeout-then-succeed"
    result = run_apply(tmp_path, client)
    assert result.receipt.operations[0].outcome == "applied-after-safe-retry"
    assert client.attempts["task-wash-dishes-id"] == 2


def test_repeated_ambiguous_timeouts_stop_after_bounded_safe_retries(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    client.timeout_modes["task-wash-dishes-id"] = "always-timeout"
    with pytest.raises(PartialMutationError, match="0/4"):
        run_apply(tmp_path, client)
    receipt = HistoryStore(tmp_path).load(next(tmp_path.glob("partial-*.json")))
    assert receipt.operations[0].status == "unknown"
    assert client.attempts["task-wash-dishes-id"] == 4


def test_success_response_without_state_change_is_partial_unknown(
    tmp_path: Path, documents: dict
) -> None:
    client = InMemoryMarvin(documents)
    client.timeout_modes["task-wash-dishes-id"] = "success-without-write"
    with pytest.raises(PartialMutationError, match="did not verify"):
        run_apply(tmp_path, client)
    receipt = HistoryStore(tmp_path).load(next(tmp_path.glob("partial-*.json")))
    assert receipt.operations[0].status == "unknown"
