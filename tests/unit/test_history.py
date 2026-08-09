from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marvin_pilot.errors import HistoryError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.history import HistoryStore, receipt_hash
from marvin_pilot.plan_io import parse_plan_bytes, plan_digest
from marvin_pilot.preflight import preflight_plan

NOW_MS = 1_786_233_600_123


class Reader:
    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = documents

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        value = self.documents.get(item_id)
        return dict(value) if value is not None else None


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 8, 22, 15, 30, 123000, tzinfo=UTC)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=1)
        return result


def make_preflight():
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    documents = {
        "task-wash-dishes-id": {
            "db": "Tasks",
            "title": "Wash the dishes",
            "day": "2026-08-08",
            "firstScheduled": "2026-01-01",
            "updatedAt": 1,
        },
        "task-dinner-id": {
            "db": "Tasks",
            "title": "Eat dinner with Jacob",
            "parentId": "unassigned",
            "updatedAt": 2,
        },
        "duplicate-task-id": {
            "db": "Tasks",
            "title": "Study chapter 3",
            "updatedAt": 1_786_221_000_123,
        },
        "people-category-id": {"db": "Categories", "title": "People"},
    }
    return plan, preflight_plan(plan, Reader(documents), now_ms=NOW_MS)


def test_pending_receipt_is_created_before_mutation_and_verifies(tmp_path: Path) -> None:
    plan, preflight = make_preflight()
    raw = json.dumps(EXAMPLE_PLAN, indent=3).encode()
    store = HistoryStore(tmp_path, now=Clock())
    store.ensure_writable()
    assert not list(tmp_path.glob(".write-check-*"))

    handle = store.begin_apply(preflight, raw, api_base_host="https://marvin.test")
    assert handle.path.name.startswith("pending-20260808T221530.123Z--")
    assert handle.path.exists()
    loaded = store.load(handle.path)
    assert loaded.status == "pending"
    assert loaded.planId == plan.planId
    assert loaded.planDigest == plan_digest(plan)
    assert loaded.sourcePlanText == raw.decode()
    assert loaded.operations[0].beforeFields["day"] == {
        "present": True,
        "value": "2026-08-08",
    }
    assert loaded.receiptHash == receipt_hash(loaded)
    text = handle.path.read_text(encoding="utf-8")
    assert "X-Full-Access-Token" not in text


def test_persist_rehashes_every_operation_transition(tmp_path: Path) -> None:
    _, preflight = make_preflight()
    store = HistoryStore(tmp_path, now=Clock())
    handle = store.begin_apply(
        preflight, json.dumps(EXAMPLE_PLAN).encode(), api_base_host="https://x"
    )
    old_hash = handle.receipt.receiptHash
    handle.receipt.status = "applying"
    handle.receipt.operations[0].status = "sending"
    store.persist(handle)
    loaded = store.load(handle.path)
    assert loaded.status == "applying"
    assert loaded.operations[0].status == "sending"
    assert loaded.receiptHash != old_hash


@pytest.mark.parametrize(
    ("status", "prefix"),
    [
        ("applied", "applied-"),
        ("partial", "partial-"),
        ("failed", "failed-"),
    ],
)
def test_finalize_renames_to_terminal_status(tmp_path: Path, status: str, prefix: str) -> None:
    _, preflight = make_preflight()
    store = HistoryStore(tmp_path, now=Clock())
    handle = store.begin_apply(
        preflight, json.dumps(EXAMPLE_PLAN).encode(), api_base_host="https://x"
    )
    pending = handle.path
    destination = store.finalize(handle, status)  # type: ignore[arg-type]
    assert destination.name.startswith(prefix)
    assert not pending.exists()
    assert store.load(destination).status == status


def test_tampering_and_malformed_receipts_are_rejected(tmp_path: Path) -> None:
    _, preflight = make_preflight()
    store = HistoryStore(tmp_path, now=Clock())
    handle = store.begin_apply(
        preflight, json.dumps(EXAMPLE_PLAN).encode(), api_base_host="https://x"
    )
    value = json.loads(handle.path.read_text(encoding="utf-8"))
    value["apiBaseHost"] = "https://tampered.test"
    handle.path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(HistoryError, match="integrity check failed"):
        store.load(handle.path)
    handle.path.write_text("not-json", encoding="utf-8")
    with pytest.raises(HistoryError, match="invalid receipt"):
        store.load(handle.path)


def test_duplicate_detection_guards_pending_applied_and_partial(tmp_path: Path) -> None:
    plan, preflight = make_preflight()
    store = HistoryStore(tmp_path, now=Clock())
    handle = store.begin_apply(
        preflight, json.dumps(EXAMPLE_PLAN).encode(), api_base_host="https://x"
    )
    duplicate = store.find_duplicate(plan_id=plan.planId, digest=plan_digest(plan))
    assert duplicate is not None and duplicate[0] == handle.path
    store.finalize(handle, "failed")
    assert store.find_duplicate(plan_id=plan.planId, digest=plan_digest(plan)) is None


def test_history_listing_and_latest_are_deterministic(tmp_path: Path) -> None:
    _, preflight = make_preflight()
    clock = Clock()
    store = HistoryStore(tmp_path, now=clock)
    first = store.begin_apply(
        preflight, json.dumps(EXAMPLE_PLAN).encode(), api_base_host="https://x"
    )
    store.finalize(first, "failed")
    second = store.begin_apply(
        preflight, json.dumps(EXAMPLE_PLAN).encode(), api_base_host="https://x"
    )
    store.finalize(second, "failed")
    paths = store.list_paths()
    assert paths == sorted(paths, reverse=True)
    latest = store.latest()
    assert latest is not None
    assert latest[0] == second.path


def test_empty_history_has_no_latest_or_duplicates(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "missing")
    assert store.list_paths() == []
    assert store.latest() is None
    assert store.find_duplicate(plan_id="id", digest="digest") is None


def test_finalize_interrupted_journal_with_no_ambiguous_send(tmp_path: Path) -> None:
    _, preflight = make_preflight()
    store = HistoryStore(tmp_path, now=Clock())
    handle = store.begin_apply(
        preflight, json.dumps(EXAMPLE_PLAN).encode(), api_base_host="https://x"
    )
    handle.receipt.status = "applying"
    handle.receipt.operations[0].status = "applied"
    handle.receipt.operations[1].status = "checking"
    store.persist(handle)

    destination = store.finalize_interrupted(handle.path)
    finalized = store.load(destination)

    assert destination.name.startswith("partial-")
    assert finalized.status == "partial"
    assert finalized.operations[0].status == "applied"
    assert finalized.operations[1].status == "failed"
    assert finalized.operations[1].outcome == "interrupted-before-send"


def test_finalize_interrupted_refuses_ambiguous_send_state(tmp_path: Path) -> None:
    _, preflight = make_preflight()
    store = HistoryStore(tmp_path, now=Clock())
    handle = store.begin_apply(
        preflight, json.dumps(EXAMPLE_PLAN).encode(), api_base_host="https://x"
    )
    handle.receipt.status = "applying"
    handle.receipt.operations[0].status = "sending"
    store.persist(handle)

    with pytest.raises(HistoryError, match="ambiguous send state"):
        store.finalize_interrupted(handle.path)
    assert handle.path.exists()
