from __future__ import annotations

import copy
import json

import pytest

from marvin_pilot.compiler import compile_complete, compile_create, compile_update
from marvin_pilot.errors import LivePreconditionError, PlanSemanticError
from marvin_pilot.models.plan_v1 import CompleteOperation, CreateOperation
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.preflight import preflight_plan
from marvin_pilot.visualizer import build_plan_view

NOW_MS = 1_786_233_600_123
ROOT_ID = "11111111-1111-4111-8111-111111111111"
CHILD_ID = "22222222-2222-4222-8222-222222222222"


class FakeReader:
    def __init__(self, documents: dict[str, dict] | None = None) -> None:
        self.documents = copy.deepcopy(documents or {})

    def get_doc(self, item_id: str) -> dict | None:
        document = self.documents.get(item_id)
        return copy.deepcopy(document) if document is not None else None

    def get_labels(self) -> list[dict]:
        return []


def encode_plan(operations: list[dict]) -> bytes:
    return json.dumps(
        {
            "schemaVersion": 1,
            "planId": "33333333-3333-4333-8333-333333333333",
            "createdAt": "2026-08-11T12:00:00-07:00",
            "summary": "Exercise project operations and historical completion.",
            "operations": operations,
        }
    ).encode()


def project_create(
    *,
    operation_id: str = "create-project",
    target_id: str = ROOT_ID,
    title: str = "Pilot project",
    parent: dict | None = None,
    depends_on: list[str] | None = None,
) -> dict:
    after = {"title": title}
    if parent is not None:
        after["parent"] = parent
    return {
        "operationId": operation_id,
        "action": "create",
        "target": {"type": "project", "id": target_id},
        "reason": "Project CRUD coverage.",
        "dependsOnOperations": depends_on or [],
        "after": after,
    }


def project_complete() -> dict:
    return {
        "operationId": "complete-project",
        "action": "complete",
        "target": {"type": "project", "id": "project-existing", "title": "Shipped"},
        "reason": "The final deliverable shipped on this date.",
        "completedAt": "2026-07-23T18:30:00-07:00",
        "expectedUpdatedAt": 100,
    }


def test_project_create_compiles_categories_document() -> None:
    operation = parse_plan_bytes(encode_plan([project_create()])).operations[0]
    assert isinstance(operation, CreateOperation)
    compiled = compile_create(operation, NOW_MS)

    assert compiled.payload == {
        "_id": ROOT_ID,
        "db": "Categories",
        "type": "project",
        "done": False,
        "day": None,
        "parentId": "unassigned",
        "createdAt": NOW_MS,
        "updatedAt": NOW_MS,
        "title": "Pilot project",
        "fieldUpdates": {"title": NOW_MS},
    }
    assert compiled.desired_fields["type"] == "project"


def test_nested_project_create_requires_order_and_operation_dependency() -> None:
    child = project_create(
        operation_id="create-child",
        target_id=CHILD_ID,
        title="Child project",
        parent={"id": ROOT_ID, "title": "Pilot project"},
        depends_on=["create-project"],
    )
    plan = parse_plan_bytes(encode_plan([project_create(), child]))
    result = preflight_plan(plan, FakeReader(), now_ms=NOW_MS)
    assert result.operations[1].compiled.payload["parentId"] == ROOT_ID

    child_without_dependency = copy.deepcopy(child)
    child_without_dependency["dependsOnOperations"] = []
    with pytest.raises(PlanSemanticError, match="add it to dependsOnOperations"):
        parse_plan_bytes(encode_plan([project_create(), child_without_dependency]))

    future_child = copy.deepcopy(child)
    future_child["dependsOnOperations"] = []
    with pytest.raises(PlanSemanticError, match="created later"):
        parse_plan_bytes(encode_plan([future_child, project_create()]))


def test_complete_compiles_historical_dates_for_tasks_and_projects() -> None:
    project = parse_plan_bytes(encode_plan([project_complete()])).operations[0]
    assert isinstance(project, CompleteOperation)
    project_compiled = compile_complete(project, {"done": False}, NOW_MS)
    project_setters = {
        setter["key"]: setter["val"] for setter in project_compiled.payload["setters"]
    }
    expected_completed_ms = 1_784_856_600_000
    assert project_setters == {
        "done": True,
        "fieldUpdates.done": expected_completed_ms,
        "doneDate": "2026-07-23",
        "fieldUpdates.doneDate": expected_completed_ms,
        "updatedAt": NOW_MS,
    }
    assert project_compiled.desired_fields == {"done": True, "doneDate": "2026-07-23"}

    task_value = project_complete()
    task_value["target"]["type"] = "task"
    task = parse_plan_bytes(encode_plan([task_value])).operations[0]
    task_compiled = compile_complete(task, {"done": False}, NOW_MS)
    task_setters = {setter["key"]: setter["val"] for setter in task_compiled.payload["setters"]}
    assert task_setters["fieldUpdates.done"] == expected_completed_ms
    assert task_setters["doneAt"] == expected_completed_ms
    assert task_setters["fieldUpdates.doneAt"] == expected_completed_ms
    assert "doneDate" not in task_setters
    assert task_compiled.desired_fields == {"done": True, "doneAt": expected_completed_ms}


def test_project_preflight_validates_document_type_and_open_state() -> None:
    plan = parse_plan_bytes(encode_plan([project_complete()]))
    live = {
        "project-existing": {
            "_id": "project-existing",
            "_rev": "1-project",
            "db": "Categories",
            "type": "project",
            "title": "Shipped",
            "done": False,
            "updatedAt": 100,
        }
    }
    result = preflight_plan(plan, FakeReader(live), now_ms=NOW_MS)
    assert result.operations[0].compiled.action == "complete"

    live["project-existing"]["type"] = "category"
    with pytest.raises(LivePreconditionError, match="not a live project"):
        preflight_plan(plan, FakeReader(live), now_ms=NOW_MS)

    live["project-existing"]["type"] = "project"
    live["project-existing"]["done"] = True
    with pytest.raises(LivePreconditionError, match="already completed"):
        preflight_plan(plan, FakeReader(live), now_ms=NOW_MS)


def test_project_rename_move_and_trash_compile_through_preflight() -> None:
    live = {
        "project-existing": {
            "_id": "project-existing",
            "_rev": "1-project",
            "db": "Categories",
            "type": "project",
            "title": "Original project",
            "parentId": "old-parent",
            "done": False,
            "updatedAt": 100,
        },
        "old-parent": {
            "_id": "old-parent",
            "db": "Categories",
            "type": "category",
            "title": "Old parent",
        },
    }
    update = {
        "operationId": "rename-move-project",
        "action": "update",
        "target": {
            "type": "project",
            "id": "project-existing",
            "title": "Original project",
        },
        "reason": "Give the project its final name and return it to Inbox.",
        "before": {
            "title": "Original project",
            "parent": {"id": "old-parent", "title": "Old parent"},
        },
        "after": {
            "title": "Renamed project",
            "parent": {"id": "unassigned", "title": "Inbox"},
        },
        "expectedUpdatedAt": 100,
    }
    update_result = preflight_plan(
        parse_plan_bytes(encode_plan([update])), FakeReader(live), now_ms=NOW_MS
    )
    assert update_result.operations[0].compiled.desired_fields == {
        "title": "Renamed project",
        "parentId": "unassigned",
    }

    trash = {
        "operationId": "trash-project",
        "action": "trash",
        "target": {
            "type": "project",
            "id": "project-existing",
            "title": "Original project",
        },
        "reason": "Exercise reversible project Trash.",
        "expectedUpdatedAt": 100,
    }
    trash_result = preflight_plan(
        parse_plan_bytes(encode_plan([trash])), FakeReader(live), now_ms=NOW_MS
    )
    assert trash_result.operations[0].compiled.endpoint == "doc/delete"
    assert trash_result.operations[0].compiled.payload == {"itemId": "project-existing"}


def test_project_move_rejects_self_and_descendant_parent_cycles() -> None:
    live = {
        "project-existing": {
            "_id": "project-existing",
            "db": "Categories",
            "type": "project",
            "title": "Parent",
            "parentId": "unassigned",
            "done": False,
            "updatedAt": 100,
        },
        "child-project": {
            "_id": "child-project",
            "db": "Categories",
            "type": "project",
            "title": "Child",
            "parentId": "project-existing",
        },
    }

    def move_to(parent_id: str, parent_title: str) -> dict:
        return {
            "operationId": "move-project",
            "action": "update",
            "target": {
                "type": "project",
                "id": "project-existing",
                "title": "Parent",
            },
            "reason": "Exercise project hierarchy safeguards.",
            "before": {"parent": {"id": "unassigned", "title": "Inbox"}},
            "after": {"parent": {"id": parent_id, "title": parent_title}},
        }

    for parent_id, parent_title in (
        ("project-existing", "Parent"),
        ("child-project", "Child"),
    ):
        with pytest.raises(LivePreconditionError, match="project parent cycle"):
            preflight_plan(
                parse_plan_bytes(encode_plan([move_to(parent_id, parent_title)])),
                FakeReader(live),
                now_ms=NOW_MS,
            )


@pytest.mark.parametrize("action", ["create", "update"])
def test_project_parent_under_root_level_category_is_accepted(action: str) -> None:
    live = {
        "project-existing": {
            "_id": "project-existing",
            "_rev": "1-project",
            "db": "Categories",
            "type": "project",
            "title": "Movable project",
            "parentId": "unassigned",
            "done": False,
            "updatedAt": 100,
        },
        "nested-category": {
            "_id": "nested-category",
            "db": "Categories",
            "type": "category",
            "title": "Nested category",
            "parentId": "top-category",
        },
        "top-category": {
            "_id": "top-category",
            "db": "Categories",
            "type": "category",
            "title": "Top category",
            "parentId": "root",
        },
    }
    if action == "create":
        operation = project_create(parent={"id": "nested-category", "title": "Nested category"})
    else:
        operation = {
            "operationId": "move-project",
            "action": "update",
            "target": {
                "type": "project",
                "id": "project-existing",
                "title": "Movable project",
            },
            "reason": "Move beneath a real top-level category hierarchy.",
            "before": {"parent": {"id": "unassigned", "title": "Inbox"}},
            "after": {"parent": {"id": "nested-category", "title": "Nested category"}},
            "expectedUpdatedAt": 100,
        }

    result = preflight_plan(
        parse_plan_bytes(encode_plan([operation])), FakeReader(live), now_ms=NOW_MS
    )
    assert result.operations[0].compiled.desired_fields["parentId"] == "nested-category"


@pytest.mark.parametrize(
    ("broken_document", "reason"),
    [
        (None, "not found"),
        ({"_id": "missing-category", "db": "Tasks", "title": "Wrong type"}, "db='Tasks'"),
    ],
)
def test_project_parent_reports_the_broken_ancestry_chain(
    broken_document: dict | None, reason: str
) -> None:
    live = {
        "nested-category": {
            "_id": "nested-category",
            "db": "Categories",
            "type": "category",
            "title": "Nested category",
            "parentId": "missing-category",
        }
    }
    if broken_document is not None:
        live["missing-category"] = broken_document
    operation = project_create(parent={"id": "nested-category", "title": "Nested category"})

    with pytest.raises(LivePreconditionError) as raised:
        preflight_plan(parse_plan_bytes(encode_plan([operation])), FakeReader(live), now_ms=NOW_MS)

    message = str(raised.value)
    assert "'nested-category' -> 'missing-category'" in message
    assert reason in message
    assert "'root'" not in message


def test_project_rules_reject_task_only_fields_and_future_completion() -> None:
    create = project_create()
    create["after"]["dependencies"] = ["task-a"]
    with pytest.raises(PlanSemanticError, match="task-only project field"):
        parse_plan_bytes(encode_plan([create]))

    for unsupported in ("masterRank", "starPriority"):
        create = project_create()
        create["after"][unsupported] = 1 if unsupported == "masterRank" else "red"
        with pytest.raises(PlanSemanticError, match="task-only project field"):
            parse_plan_bytes(encode_plan([create]))

    complete = project_complete()
    complete["completedAt"] = "2026-08-12T12:00:00-07:00"
    with pytest.raises(PlanSemanticError, match="later than the plan"):
        parse_plan_bytes(encode_plan([complete]))


def test_unscheduling_project_uses_native_null_day() -> None:
    operation = {
        "operationId": "unschedule-project",
        "action": "update",
        "target": {"type": "project", "id": "project-existing", "title": "Project"},
        "reason": "Use the native project clear representation.",
        "before": {"scheduledDate": "2026-08-10"},
        "after": {"scheduledDate": None},
    }
    parsed = parse_plan_bytes(encode_plan([operation])).operations[0]
    compiled = compile_update(parsed, {"day": "2026-08-10"}, NOW_MS)
    setters = {setter["key"]: setter["val"] for setter in compiled.payload["setters"]}
    assert setters["day"] is None


def test_visualizer_exposes_project_type_and_completion() -> None:
    plan = parse_plan_bytes(encode_plan([project_complete()]))
    view = build_plan_view(plan)
    operation = view.operations[0]

    assert view.counts == {"create": 0, "update": 0, "complete": 1, "trash": 0}
    assert operation.target_type == "project"
    assert operation.before is not None
    assert operation.after is not None
    assert operation.diffs[0].label == "Project state"
    assert operation.diffs[0].after.summary == "Completed at 2026-07-23T18:30:00-07:00"
