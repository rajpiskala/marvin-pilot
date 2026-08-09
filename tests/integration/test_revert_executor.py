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
    assert client.documents["duplicate-task-id"]["deletedAt"] is None
    assert client.documents["duplicate-task-id"]["restoredAt"] == REVERT_MS
    assert client.documents["40d06376-9125-4e9e-a6bd-631cb0e6dc55"]["deletedAt"] == REVERT_MS


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
    assert client.documents["duplicate-task-id"]["deletedAt"] == APPLY_MS


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
