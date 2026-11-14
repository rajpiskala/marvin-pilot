from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from marvin_pilot.errors import PlanSemanticError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import canonical_plan_bytes, parse_plan_bytes
from marvin_pilot.text_review import text_choices
from marvin_pilot.text_review_store import TextReviewStore, review_lock

CONTEXT = {"timeZone": "America/Los_Angeles", "localDate": "2026-11-13"}


def make_store(tmp_path: Path):
    raw = copy.deepcopy(EXAMPLE_PLAN)
    raw["operations"][2]["after"]["subtasks"] = [
        {"id": "first", "title": "First step", "done": False},
        {"id": "second", "title": "Second step", "done": True},
    ]
    plan = parse_plan_bytes(json.dumps(raw).encode())
    return TextReviewStore(tmp_path / "source.json", plan)


def title(text="Edited title"):
    return {"operationId": "create-email-follow-up", "field": "title", "text": text}


def subtask(identifier="first", text="Edited step"):
    return {
        "operationId": "create-email-follow-up",
        "field": "subtaskTitle",
        "subtaskId": identifier,
        "text": text,
    }


def patch(store, edits, *, client=None, sequence=1, context=None):
    return store.patch(
        edits, context=context or CONTEXT, client_id=client or str(uuid4()), sequence=sequence
    )


def test_independent_writers_merge_and_stale_tab_overwrites_only_its_field(tmp_path):
    store = make_store(tmp_path)
    before = canonical_plan_bytes(store.plan)
    assert store.snapshot()["version"] == 0
    assert list(tmp_path.iterdir()) == []  # Read-only visits create nothing.
    other = TextReviewStore(tmp_path / "source.json", store.plan)
    patch(store, [title("A by tab A")])
    patch(
        other,
        [subtask(text="B by tab B")],
        context={"timeZone": "Europe/London", "localDate": "2026-11-14"},
    )
    patch(store, [title("A by stale tab D")])
    fresh = TextReviewStore(tmp_path / "source.json", store.plan).snapshot()
    assert fresh["version"] == 3
    assert [e["text"] for e in fresh["edits"]] == ["A by stale tab D", "B by tab B"]
    assert fresh["edits"][0]["referenceContext"] == CONTEXT
    assert fresh["edits"][1]["referenceContext"]["timeZone"] == "Europe/London"
    assert canonical_plan_bytes(store.plan) == before
    assert store.snapshot() == fresh  # Another process invalidates the read cache.


def test_restore_original_removes_only_the_selected_field(tmp_path):
    store = make_store(tmp_path)
    patch(store, [title(), subtask(), subtask("second", "Revised second")])
    original = next(
        c["originalText"]
        for c in text_choices(store.plan)
        if c["field"] == "title" and c["operationId"] == "create-email-follow-up"
    )
    result = patch(store, [title(original)])
    assert [e["subtaskId"] for e in result["edits"]] == ["first", "second"]
    result = patch(store, [subtask(text="First step"), subtask("second", "Second step")])
    assert result["edits"] == []
    assert result["requiresAIReconciliation"] is False
    assert TextReviewStore(tmp_path / "source.json", store.plan).snapshot()["edits"] == []


def test_lost_response_retry_and_out_of_order_requests_cannot_clobber_newer_writer(tmp_path):
    store = make_store(tmp_path)
    client = str(uuid4())
    first = patch(store, [title("First")], client=client)
    assert patch(store, [title("First")], client=client) == first
    patch(store, [title("From another tab")])
    assert patch(store, [title("First")], client=client)["edits"][0]["text"] == "From another tab"
    patch(store, [title("Second")], client=client, sequence=2)
    assert patch(store, [title("First")], client=client)["edits"][0]["text"] == "Second"
    with pytest.raises(PlanSemanticError, match="reused"):
        patch(store, [title("Different payload")], client=client, sequence=2)


def test_concurrent_stores_do_not_lose_unrelated_fields(tmp_path):
    store = make_store(tmp_path)
    other = TextReviewStore(tmp_path / "source.json", store.plan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(patch, store, [title()])
        b = pool.submit(patch, other, [subtask()])
        a.result()
        b.result()
    assert len(store.snapshot()["edits"]) == 2


@pytest.mark.parametrize(
    "client,sequence",
    [(None, 1), ("bad", 1), (str(uuid4()), True), (str(uuid4()), 0), (str(uuid4()), 2**53)],
)
def test_invalid_writer_identity_cannot_write(tmp_path, client, sequence):
    store = make_store(tmp_path)
    with pytest.raises(PlanSemanticError):
        store.patch([title()], context=CONTEXT, client_id=client, sequence=sequence)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("damage", ["syntax", "digest", "field", "context", "client", "version"])
def test_corrupted_feedback_is_not_silently_replaced(tmp_path, damage):
    store = make_store(tmp_path)
    patch(store, [title()])
    value = json.loads(store.path.read_bytes())
    if damage == "digest":
        value["sourceDigest"] = "sha256:wrong"
    if damage == "field":
        value["edits"][0]["targetId"] = "wrong"
    if damage == "context":
        value["edits"][0]["referenceContext"] = {}
    if damage == "client":
        value["clients"] = {"invalid": {}}
    if damage == "version":
        value["version"] = True
    raw = b"not json" if damage == "syntax" else json.dumps(value).encode()
    store.path.write_bytes(raw)
    with pytest.raises(PlanSemanticError):
        patch(store, [subtask()])
    assert store.path.read_bytes() == raw


def test_lock_times_out_without_writing_feedback(tmp_path):
    store = make_store(tmp_path)
    with (
        review_lock(store.lock_path),
        pytest.raises(PlanSemanticError, match="busy"),
        review_lock(store.lock_path, timeout=0),
    ):
        pytest.fail("The lock must be exclusive")


def test_write_failure_keeps_previous_saved_fields(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    patch(store, [title()])
    original = store.path.read_bytes()

    def fail(*_):
        raise OSError("disk full")

    monkeypatch.setattr("marvin_pilot.text_review_store.atomic_write_bytes", fail)
    with pytest.raises(OSError, match="disk full"):
        patch(store, [subtask()])
    assert store.path.read_bytes() == original
