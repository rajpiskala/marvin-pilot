from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from marvin_pilot.database import CouchDatabase, DatabaseCredentials, DatabaseMarvinClient
from marvin_pilot.database_executor import database_batch_schedule, independent_batches
from marvin_pilot.errors import LivePreconditionError, PartialMutationError, PlanSemanticError
from marvin_pilot.executor import execute_apply
from marvin_pilot.history import HistoryStore
from marvin_pilot.marvin_client import RequestPacer
from marvin_pilot.plan_io import canonical_plan_bytes
from marvin_pilot.preflight import validate_plan_live
from marvin_pilot.prepare import SnapshotReader, parse_draft_bytes, prepare_draft
from marvin_pilot.reverter import execute_revert

NOW = 1_788_550_400_000


class CouchServer:
    def __init__(self, count=2):
        self.documents = {
            f"task-{i}": {
                "_id": f"task-{i}",
                "_rev": "1-" + "a" * 32,
                "db": "Tasks",
                "title": f"Synthetic task {i}",
                "parentId": "unassigned",
                "updatedAt": 42,
                "unknownNativeField": {"preserve": True},
            }
            for i in range(count)
        }
        self.deleted = {}
        self.calls = []
        self.batches = []
        self.conflict = None
        self.after_write = None
        self.ambiguous = False

    def handle(self, request):
        path = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append(path)
        if path == "_all_docs":
            rows = []
            for identifier in body["keys"]:
                if identifier in self.documents:
                    rows.append({"key": identifier, "doc": deepcopy(self.documents[identifier])})
                elif identifier in self.deleted:
                    rows.append(
                        {
                            "key": identifier,
                            "value": {"deleted": True, "rev": self.deleted[identifier]},
                        }
                    )
                else:
                    rows.append({"key": identifier, "error": "not_found"})
            return httpx.Response(200, json={"rows": rows})
        if path == "_find":

            def matches(doc):
                for key, expected in body["selector"].items():
                    value = doc.get(key)
                    if isinstance(expected, dict):
                        if "$in" in expected and value not in expected["$in"]:
                            return False
                        if "$ne" in expected and value == expected["$ne"]:
                            return False
                    elif value != expected:
                        return False
                return True

            return httpx.Response(
                200, json={"docs": [deepcopy(d) for d in self.documents.values() if matches(d)]}
            )
        assert path == "_bulk_docs"
        self.batches.append(deepcopy(body["docs"]))
        rows = []
        for doc in body["docs"]:
            identifier = doc["_id"]
            current = self.documents.get(identifier)
            revision = current["_rev"] if current else self.deleted.get(identifier)
            restoring_deleted = current is None and identifier in self.deleted
            if (
                identifier == self.conflict
                or (restoring_deleted and "_rev" in doc)
                or (not restoring_deleted and doc.get("_rev") != revision)
            ):
                rows.append({"id": identifier, "error": "conflict"})
                continue
            next_rev = f"{int(revision.split('-')[0]) + 1 if revision else 1}-" + "b" * 32
            if doc.get("_deleted"):
                self.documents.pop(identifier, None)
                self.deleted[identifier] = next_rev
            else:
                self.documents[identifier] = deepcopy(doc) | {"_rev": next_rev}
                self.deleted.pop(identifier, None)
            rows.append({"id": identifier, "ok": True, "rev": next_rev})
        if self.after_write:
            self.after_write(self)
        if self.ambiguous:
            return httpx.Response(503)
        return httpx.Response(201, json=rows)


@pytest.fixture
def engine():
    server = CouchServer()
    api = SimpleNamespace(
        api_base_host="https://serv.amazingmarvin.com",
        api_base_url="https://serv.amazingmarvin.com/api",
        check_connection=lambda: SimpleNamespace(
            account_user_id="123456", account_email="pilot@example.com"
        ),
        close=lambda: None,
        update_doc=lambda *_: pytest.fail("ordinary updates must not use API"),
    )
    credentials = DatabaseCredentials(
        "123456", "pilot@example.com", "https://db.test", "test-db", "user", "synthetic-secret"
    )
    database = CouchDatabase(
        credentials, transport=httpx.MockTransport(server.handle), pacer=RequestPacer(0)
    )
    client = DatabaseMarvinClient(api, database)
    yield server, client
    client.close()


def make_plan(server, operations=None):
    operations = operations or [
        {
            "operationId": f"edit-{i}",
            "action": "update",
            "target": {"type": "task", "id": identifier},
            "after": {"title": f"Updated task {i}"},
            "reason": "Exercise the experimental engine.",
        }
        for i, identifier in enumerate(server.documents)
    ]
    draft = parse_draft_bytes(
        json.dumps(
            {
                "draftVersion": 1,
                "summary": "Synthetic DB test",
                "expectedAccount": {"userId": "123456", "email": "pilot@example.com"},
                "operations": operations,
            }
        ).encode()
    )
    return prepare_draft(draft, SnapshotReader(list(server.documents.values())), now_ms=NOW)


def apply(plan, client, history, approve=lambda _: True):
    return execute_apply(
        plan,
        canonical_plan_bytes(plan),
        client=client,
        history=history,
        approve=approve,
        now_ms=lambda: NOW,
    )


def test_one_hundred_updates_use_four_database_requests_and_keep_unknown_fields(engine, tmp_path):
    server, client = engine
    server.documents = CouchServer(100).documents
    result = apply(make_plan(server), client, HistoryStore(tmp_path))
    assert result.receipt.status == "applied"
    assert len(server.batches) == 1 and len(server.batches[0]) == 100
    assert server.calls == ["_all_docs", "_all_docs", "_bulk_docs", "_all_docs"]
    assert all(d["unknownNativeField"] == {"preserve": True} for d in server.documents.values())
    assert all(
        r.beforeDocument is not None and r.status == "applied" for r in result.receipt.operations
    )


def add_project(server, project_id, parent="root"):
    server.documents[project_id] = {
        "_id": project_id,
        "_rev": "1-" + "a" * 32,
        "db": "Categories",
        "type": "project",
        "title": "Before " + project_id,
        "parentId": parent,
        "updatedAt": 42,
    }


def edit(id_, *, kind="task", depends=(), suffix=""):
    return {
        "operationId": id_ + suffix,
        "action": "update",
        "target": {"type": kind, "id": id_},
        "after": {"title": "Renamed " + id_ + suffix},
        "dependsOnOperations": list(depends),
        "reason": "Exercise batching with independent and ordered edits.",
    }


def test_plain_project_edit_bridges_task_batches_and_is_receipt_reversible(engine, tmp_path):
    server, client = engine
    add_project(server, "project-a")
    originals = {key: doc["title"] for key, doc in server.documents.items()}
    plan = make_plan(server, [edit("task-0"), edit("project-a", kind="project"), edit("task-1")])
    validation = validate_plan_live(plan, client, now_ms=NOW)
    assert validation.valid
    assert [
        len(b.operations) for b in database_batch_schedule(validation.operations, optimize=False)
    ] == [1, 1, 1]
    journal = HistoryStore(tmp_path)
    applied = apply(plan, client, journal)
    assert [len(b) for b in server.batches] == [3]
    assert all(r.status == "applied" for r in applied.receipt.operations)
    reverted = execute_revert(
        applied.receipt,
        Path(applied.receipt_path),
        [],
        client=client,
        history=journal,
        approve=lambda _: True,
        now_ms=lambda: NOW,
    )
    assert reverted.receipt.status == "reverted"
    assert {key: doc["title"] for key, doc in server.documents.items()} == originals


def test_independent_edit_moves_up_but_parent_reference_stays_separate(engine, tmp_path):
    server, client = engine
    add_project(server, "project-a")
    server.documents["task-0"]["parentId"] = "project-a"
    project_edit = edit("project-a", kind="project") | {"after": {"scheduledDate": "2026-09-07"}}
    plan = make_plan(server, [project_edit, edit("task-0"), edit("task-1")])
    checked = validate_plan_live(plan, client, now_ms=NOW)
    assert checked.valid
    batches = list(database_batch_schedule(checked.operations))
    assert [[item.operation.operationId for item in b.operations] for b in batches] == [
        ["project-a", "task-1"],
        ["task-0"],
    ]
    assert batches[1].boundary == "parent-reference"
    result = apply(plan, client, HistoryStore(tmp_path))
    assert [len(b) for b in server.batches] == [2, 1]
    assert {item.operationId: item.applyIndex for item in result.receipt.operations} == {
        "project-a": 1,
        "task-0": 3,
        "task-1": 2,
    }


def test_reordered_batch_conflict_stops_dependency_without_replaying_success(engine, tmp_path):
    server, client = engine
    add_project(server, "project-a")
    plan = make_plan(
        server,
        [edit("project-a", kind="project"), edit("task-0", depends=["project-a"]), edit("task-1")],
    )
    server.conflict = "project-a"
    journal = HistoryStore(tmp_path)
    with pytest.raises(PartialMutationError):
        apply(plan, client, journal)
    assert [[d["_id"] for d in batch] for batch in server.batches] == [["project-a", "task-1"]]
    path, receipt = journal.find_duplicate(plan_id=plan.planId, digest="not-used")
    assert path.exists() and receipt.status == "partial"
    assert {op.operationId: op.status for op in receipt.operations} == {
        "project-a": "failed",
        "task-0": "not-started",
        "task-1": "applied",
    }
    assert server.documents["task-0"]["title"] == "Synthetic task 0"


def test_one_hundred_forty_mixed_mutations_share_one_write_after_fresh_hierarchy(engine, tmp_path):
    server, client = engine
    server.documents = CouchServer(140).documents
    for document in server.documents.values():
        document["parentId"] = "project"
    server.documents["project"] = {
        "_id": "project",
        "_rev": "1-a",
        "db": "Categories",
        "type": "project",
        "title": "Synthetic project",
        "parentId": "unassigned",
    }
    operations = [
        {
            "operationId": f"{'edit' if i < 40 else 'trash'}-{i}",
            "action": "update" if i < 40 else "trash",
            "target": {"type": "task", "id": f"task-{i}"},
            **({"after": {"title": f"Updated task {i}"}} if i < 40 else {}),
            "reason": "Exercise one mixed batch.",
        }
        for i in range(140)
    ]
    result = apply(make_plan(server, operations), client, HistoryStore(tmp_path))
    assert server.calls == ["_all_docs", "_all_docs", "_all_docs", "_bulk_docs", "_all_docs"]
    assert [len(batch) for batch in server.batches] == [140]
    assert len(server.deleted) == 100
    assert all(operation.beforeDocument for operation in result.receipt.operations)


def test_known_hierarchy_keys_are_hints_not_stale_documents(engine):
    server, client = engine
    plan = make_plan(server)
    client.prefetch_operations(plan.operations)
    server.documents["task-0"]["parentId"] = "moved"
    server.documents["moved"] = {
        "_id": "moved",
        "_rev": "1-z",
        "db": "Categories",
        "title": "Fresh parent",
    }
    server.calls.clear()
    client.prefetch_operations(plan.operations)
    assert server.calls == ["_all_docs", "_all_docs"]
    assert client.get_doc("task-0")["parentId"] == "moved"
    assert client.get_doc("moved")["title"] == "Fresh parent"


def test_projected_body_limit_splits_independent_batches(engine):
    server, client = engine
    result = validate_plan_live(make_plan(server), client, now_ms=NOW)
    assert result.valid
    assert [len(batch) for batch in independent_batches(result.operations, maximum_bytes=400)] == [
        1,
        1,
    ]


def test_bulk_preflight_returns_every_target_error_without_writes(engine):
    server, client = engine
    plan = make_plan(server)
    for doc in server.documents.values():
        doc["title"] = "Concurrent edit"
    result = validate_plan_live(plan, client, now_ms=NOW)
    assert len(result.diagnostics) == 2
    assert server.calls == ["_all_docs"] and not server.batches


def test_account_pin_is_mandatory_for_experimental_writes(engine, tmp_path):
    server, client = engine
    plan = make_plan(server).model_copy(update={"expectedAccount": None})
    with pytest.raises(PlanSemanticError, match="expectedAccount"):
        apply(plan, client, HistoryStore(tmp_path))
    assert not server.calls


def test_partial_bulk_conflict_records_success_and_stops_dependents(engine, tmp_path):
    server, client = engine
    plan = make_plan(server)
    server.conflict = "task-1"
    with pytest.raises(PartialMutationError):
        apply(plan, client, HistoryStore(tmp_path))
    paths = HistoryStore(tmp_path).list_paths()
    assert len(paths) == 1
    receipt = json.loads(paths[0].read_text())
    assert receipt["status"] == "partial"
    assert [op["status"] for op in receipt["operations"]] == ["applied", "failed"]
    assert server.documents["task-0"]["title"] == "Updated task 0"
    assert server.documents["task-1"]["title"] == "Synthetic task 1"


def test_ambiguous_batch_reconciles_without_resending(engine, tmp_path):
    server, client = engine
    server.ambiguous = True
    result = apply(make_plan(server), client, HistoryStore(tmp_path))
    assert result.receipt.status == "applied" and len(server.batches) == 1
    assert all(op.outcome == "applied-database-reconciled" for op in result.receipt.operations)


def test_concurrent_edit_after_send_is_not_adopted_as_our_revision(engine, tmp_path):
    server, client = engine

    def edit(server):
        server.documents["task-0"]["_rev"] = "3-" + "c" * 32
        server.documents["task-0"]["unrelated"] = "Human change"

    server.after_write = edit
    with pytest.raises(PartialMutationError):
        apply(make_plan(server), client, HistoryStore(tmp_path))
    assert server.documents["task-0"]["unrelated"] == "Human change"


def test_approval_delay_rechecks_every_revision(engine, tmp_path):
    server, client = engine

    def approve(_):
        server.documents["task-0"]["_rev"] = "2-" + "c" * 32
        return True

    with pytest.raises(PartialMutationError):
        apply(make_plan(server), client, HistoryStore(tmp_path), approve)
    assert not server.batches


def test_unresolved_conflict_is_a_preflight_error(engine):
    server, client = engine
    plan = make_plan(server)
    server.documents["task-0"]["_conflicts"] = ["2-other"]
    result = validate_plan_live(plan, client, now_ms=NOW)
    assert result.diagnostics[0].check == "database-conflicts"
    assert not server.batches


def test_update_receipt_reverts_without_losing_native_fields(engine, tmp_path):
    server, client = engine
    history = HistoryStore(tmp_path)
    result = apply(make_plan(server), client, history)
    reverted = execute_revert(
        result.receipt,
        Path(result.receipt_path),
        [],
        client=client,
        history=history,
        approve=lambda _: True,
        now_ms=lambda: NOW + 1000,
    )
    assert reverted.receipt.status == "reverted"
    assert server.documents["task-0"]["title"] == "Synthetic task 0"
    assert server.documents["task-0"]["unknownNativeField"] == {"preserve": True}


def test_delete_and_restore_same_id_use_guarded_insert_not_tombstone_update(engine, tmp_path):
    server, client = engine
    original = deepcopy(server.documents["task-0"])
    plan = make_plan(
        server,
        [
            {
                "operationId": "trash",
                "action": "trash",
                "target": {"type": "task", "id": "task-0"},
                "reason": "Test recovery.",
            }
        ],
    )
    history = HistoryStore(tmp_path)
    result = apply(plan, client, history)
    assert "task-0" not in server.documents
    assert server.batches[0][0]["D"] == 1
    execute_revert(
        result.receipt,
        Path(result.receipt_path),
        [],
        client=client,
        history=history,
        approve=lambda _: True,
        now_ms=lambda: NOW + 1000,
    )
    assert "_rev" not in server.batches[-1][0]
    assert server.documents["task-0"]["_rev"] == "3-" + "b" * 32
    assert {k: v for k, v in server.documents["task-0"].items() if k != "_rev"} == {
        k: v for k, v in original.items() if k != "_rev"
    }


def test_single_write_does_not_adopt_a_revision_changed_after_recheck(engine):
    server, client = engine
    client.get_doc("task-0")
    server.documents["task-0"]["_rev"] = "2-" + "c" * 32
    with pytest.raises(LivePreconditionError, match="refused"):
        client.update_doc("task-0", [{"key": "title", "val": "Overwrite"}])
    assert server.documents["task-0"]["title"] == "Synthetic task 0"


def test_create_parent_before_children_and_revert_in_reverse_order(engine, tmp_path):
    server, client = engine
    category = "b25703d6-307a-4502-8264-c5351265e86c"
    project = "b25703d6-307a-4502-8264-c5351265e86d"
    task = "b25703d6-307a-4502-8264-c5351265e86e"
    ops = [
        {
            "operationId": "category",
            "action": "create",
            "target": {"type": "category", "id": category},
            "after": {"title": "Synthetic category", "parent": {"id": "root"}},
            "reason": "Test root categories.",
        },
        {
            "operationId": "project",
            "action": "create",
            "target": {"type": "project", "id": project},
            "after": {"title": "Synthetic project", "parent": {"id": category}},
            "reason": "Test child project.",
            "dependsOnOperations": ["category"],
        },
        {
            "operationId": "task",
            "action": "create",
            "target": {"type": "task", "id": task},
            "after": {
                "title": "Synthetic child",
                "parent": {"id": project},
                "subtasks": [{"id": "subtask", "title": "Verify recovery"}],
            },
            "reason": "Test embedded subtasks.",
            "dependsOnOperations": ["project"],
        },
    ]
    history = HistoryStore(tmp_path)
    result = apply(make_plan(server, ops), client, history)
    assert [[doc["_id"] for doc in batch] for batch in server.batches] == [
        [category],
        [project],
        [task],
    ]
    assert len(server.documents[task]["subtasks"]) == 1
    execute_revert(
        result.receipt,
        Path(result.receipt_path),
        [],
        client=client,
        history=history,
        approve=lambda _: True,
        now_ms=lambda: NOW + 1000,
    )
    assert all(identifier not in server.documents for identifier in [task, project, category])


def test_container_with_completed_children_cannot_be_deleted(engine):
    server, client = engine
    server.documents["project"] = {
        "_id": "project",
        "_rev": "1-a",
        "db": "Categories",
        "type": "project",
        "title": "Synthetic project",
    }
    server.documents["task-0"].update(parentId="project", done=True)
    plan = make_plan(
        server,
        [
            {
                "operationId": "trash-project",
                "action": "trash",
                "target": {"type": "project", "id": "project"},
                "reason": "Ensure child safety.",
            }
        ],
    )
    result = validate_plan_live(plan, client, now_ms=NOW)
    assert any("non-empty" in d.message for d in result.diagnostics)
    assert not server.batches


def test_ambiguous_batch_with_a_later_human_edit_remains_unknown(engine, tmp_path):
    server, client = engine
    server.ambiguous = True

    def edit(server):
        server.documents["task-0"].update(_rev="3-" + "c" * 32, extra="Human edit")

    server.after_write = edit
    with pytest.raises(PartialMutationError):
        apply(make_plan(server), client, HistoryStore(tmp_path))
    receipt = HistoryStore(tmp_path).load(HistoryStore(tmp_path).list_paths()[0])
    assert receipt.operations[0].status == "unknown"
    assert len(server.batches) == 1


def test_explicit_occurrence_write_uses_api_compatibility_with_database_readback(engine, tmp_path):
    server, client = engine
    server.documents["series"] = {
        "_id": "series",
        "_rev": "1-a",
        "db": "RecurringTasks",
        "recurringType": "task",
        "title": "Synthetic series",
        "type": "daily",
        "repeatStart": "2026-09-04",
    }
    server.documents["task-0"].update(recurring=True, recurringTaskId="series", day="2026-09-04")
    calls = []

    def api_update(identifier, setters):
        calls.append(identifier)
        document = server.documents[identifier]
        for setter in setters:
            if setter["key"].startswith("fieldUpdates."):
                document.setdefault("fieldUpdates", {})[setter["key"].split(".", 1)[1]] = setter[
                    "val"
                ]
            else:
                document[setter["key"]] = setter["val"]
        document["_rev"] = "2-" + "b" * 32

    client.api.update_doc = api_update
    op = {
        "operationId": "complete-occurrence",
        "action": "complete",
        "target": {
            "type": "task",
            "id": "task-0",
            "recurrence": {
                "scope": "occurrence",
                "seriesId": "series",
                "seriesTitle": "Synthetic series",
                "scheduledDate": "2026-09-04",
            },
        },
        "completedAt": "2026-09-04T12:00:00-07:00",
        "reason": "Test compatibility routing.",
    }
    result = apply(make_plan(server, [op]), client, HistoryStore(tmp_path))
    assert calls == ["task-0"] and not server.batches
    assert result.receipt.operations[0].outcome == "applied-api-compatibility"
    assert (
        server.documents["task-1"]["done"] is not True
        if "done" in server.documents["task-1"]
        else True
    )
