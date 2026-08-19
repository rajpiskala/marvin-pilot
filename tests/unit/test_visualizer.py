from __future__ import annotations

import copy
import json
from uuid import UUID

from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.field_registry import FIELD_SPECS
from marvin_pilot.models.plan_v1 import RecurringTaskFields, TaskFields
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.visualizer import build_plan_view
from marvin_pilot.visualizer_fields import FIELD_PRESENTATIONS, presentation_for


def _parse(value: dict):
    return parse_plan_bytes(json.dumps(value).encode())


def _view(value: dict | None = None):
    return build_plan_view(_parse(value or EXAMPLE_PLAN), source_name="plan.json")


def _hierarchy_plan() -> dict:
    work = {
        "id": "category-work",
        "type": "category",
        "title": "Work",
        "emoji": "💼",
        "color": "#c69c7b",
    }
    atlas = {"id": "category-atlas", "type": "category", "title": "Project Atlas"}
    monitoring_before = {
        "id": "project-monitoring",
        "type": "project",
        "title": "Release Monitoring",
    }
    followups_id = "33333333-3333-4333-8333-333333333333"
    followups = {"id": followups_id, "type": "project", "title": "Follow-ups"}
    waiting = {"key": "waiting", "title": "Waiting", "order": 10}
    main = {"key": "main", "title": "Main", "order": 30}
    return {
        "schemaVersion": 1,
        "planId": "44444444-4444-4444-8444-444444444444",
        "createdAt": "2026-08-13T12:00:00-07:00",
        "summary": "Preview a synthetic hierarchy cleanup.",
        "reviewDisplay": {"showDaySectionsByDefault": False},
        "operations": [
            {
                "operationId": "rename-monitoring",
                "action": "update",
                "target": {
                    "type": "project",
                    "id": "project-monitoring",
                    "title": "Release Monitoring",
                },
                "reason": "Use the concise project name.",
                "before": {"title": "Release Monitoring"},
                "after": {"title": "Monitoring"},
                "display": {
                    "beforePath": [work, atlas],
                    "afterPath": [work, atlas],
                    "beforeDaySection": waiting,
                    "afterDaySection": main,
                },
            },
            {
                "operationId": "create-followups",
                "action": "create",
                "target": {"type": "project", "id": followups_id},
                "reason": "Separate follow-up work.",
                "after": {
                    "title": "Follow-ups",
                    "parent": {"id": "category-atlas", "title": "Project Atlas"},
                },
                "display": {
                    "afterPath": [work, atlas],
                    "afterDaySection": main,
                },
            },
            {
                "operationId": "move-task",
                "action": "update",
                "target": {"type": "task", "id": "task-a", "title": "Task A"},
                "reason": "Move the task into the focused project.",
                "dependsOnOperations": ["create-followups"],
                "before": {"parent": {"id": "project-monitoring", "title": "Release Monitoring"}},
                "after": {"parent": {"id": followups_id, "title": "Follow-ups"}},
                "display": {
                    "beforePath": [work, atlas, monitoring_before],
                    "afterPath": [work, atlas, followups],
                    "beforeDaySection": waiting,
                    "afterDaySection": main,
                },
            },
        ],
    }


def test_presentation_registry_covers_schema_and_compiler() -> None:
    fields = set(TaskFields.model_fields)
    assert set(FIELD_SPECS) == fields
    assert set(FIELD_PRESENTATIONS) == fields | set(RecurringTaskFields.model_fields)
    assert all(presentation_for(field).label for field in fields)
    assert presentation_for("futureMarvinField").kind == "generic"


def test_plan_view_metadata_counts_and_digest_are_deterministic() -> None:
    first = _view()
    second = _view()
    assert first == second
    assert first.source_name == "plan.json"
    assert first.schema_version == 1
    assert first.supported_schema_versions == (1,)
    assert first.counts == {"create": 1, "update": 2, "complete": 0, "trash": 1}
    assert first.total_operations == 4
    assert first.digest.startswith("sha256:")
    json.dumps(first.to_dict(), ensure_ascii=False)


def test_typed_paths_build_first_class_project_and_task_trees() -> None:
    view = _view(_hierarchy_plan())
    assert view.previews.show_day_sections_by_default is False

    work_before = view.previews.before.roots[0]
    assert (work_before.type, work_before.title, work_before.emoji, work_before.color) == (
        "category",
        "Work",
        "💼",
        "#c69c7b",
    )
    atlas_before = work_before.children[0]
    monitoring_before = atlas_before.children[0]
    assert monitoring_before.context_only is False
    assert monitoring_before.operation_id == "rename-monitoring"
    assert monitoring_before.type == "project"
    assert [child.operation_id for child in monitoring_before.children] == ["move-task"]

    work_after = view.previews.after.roots[0]
    atlas_after = work_after.children[0]
    assert [(node.title, node.operation_id) for node in atlas_after.children] == [
        ("Monitoring", "rename-monitoring"),
        ("Follow-ups", "create-followups"),
    ]
    assert [child.operation_id for child in atlas_after.children[1].children] == ["move-task"]
    assert view.previews.before.incomplete_operation_ids == ()
    assert view.operations[0].change_kinds == ("renamed",)
    assert view.operations[2].change_kinds == ("moved",)


def test_missing_paths_are_grouped_as_incomplete_instead_of_rendered_as_roots() -> None:
    value = _hierarchy_plan()
    value["operations"][0].pop("display")
    view = _view(value)
    before = view.previews.before
    assert before.incomplete_operation_ids == ("rename-monitoring",)
    incomplete = next(
        root for root in before.roots if root.id == "marvin-pilot:location-not-supplied"
    )
    assert incomplete.type == "unknown"
    assert [child.operation_id for child in incomplete.children] == ["rename-monitoring"]


def test_completion_inherits_known_ancestry_but_respects_explicit_unknown_after_path() -> None:
    category = {"id": "category-sandbox", "type": "category", "title": "Sandbox"}
    value = {
        "schemaVersion": 1,
        "planId": "77777777-7777-4777-8777-777777777777",
        "createdAt": "2026-08-16T09:00:00-07:00",
        "summary": "Preview one historical completion.",
        "operations": [
            {
                "operationId": "complete-fixture",
                "action": "complete",
                "target": {"type": "task", "id": "task-fixture", "title": "Fixture task"},
                "reason": "Record the completed fixture.",
                "completedAt": "2026-08-15T18:30:00-07:00",
                "display": {"beforePath": [category], "beforeOrder": 42},
            }
        ],
    }
    view = _view(value)
    operation = view.operations[0]
    assert operation.completed_at == "2026-08-15T18:30:00-07:00"
    assert operation.after_path_state == "path"
    assert operation.after_path == operation.before_path
    assert operation.after_order == operation.before_order == 42
    assert view.previews.before.incomplete_operation_ids == ()
    assert view.previews.after.incomplete_operation_ids == ()
    assert view.previews.after.roots[0].children[0].operation_id == "complete-fixture"

    value["operations"][0]["display"]["afterPath"] = None
    explicit_unknown = _view(value)
    assert explicit_unknown.operations[0].after_path_state == "unknown"
    assert explicit_unknown.previews.after.incomplete_operation_ids == ("complete-fixture",)


def test_completed_task_update_preserves_completion_in_both_preview_states() -> None:
    completed_at = "2026-07-23T18:30:00-07:00"
    value = _hierarchy_plan()
    operation = value["operations"][2]
    operation["display"]["existingCompletedAt"] = completed_at
    view = _view(value)

    rendered = view.operations[2]
    assert rendered.existing_completed_at == completed_at
    assert rendered.completed_at is None
    assert rendered.change_kinds == ("moved",)
    assert rendered.before is not None
    assert rendered.after is not None


def test_display_order_sorts_siblings_without_changing_operation_order() -> None:
    value = _hierarchy_plan()
    value["operations"][0]["display"]["afterOrder"] = 30
    value["operations"][1]["display"]["afterOrder"] = 10
    view = _view(value)
    atlas = view.previews.after.roots[0].children[0]
    assert [node.operation_id for node in atlas.children] == [
        "create-followups",
        "rename-monitoring",
    ]
    assert [operation.operation_id for operation in view.operations[:2]] == [
        "rename-monitoring",
        "create-followups",
    ]


def test_day_sections_are_an_optional_projection_not_hierarchy_nodes() -> None:
    view = _view(_hierarchy_plan())
    before_groups = view.previews.before.day_sections
    after_groups = view.previews.after.day_sections
    assert [(group.key, group.title, group.order) for group in before_groups] == [
        ("waiting", "Waiting", 10)
    ]
    assert [(group.key, group.title, group.order) for group in after_groups] == [
        ("main", "Main", 30)
    ]
    assert before_groups[0].operation_ids == ("rename-monitoring", "move-task")
    assert after_groups[0].operation_ids == (
        "rename-monitoring",
        "create-followups",
        "move-task",
    )
    assert view.previews.before.roots[0].title == "Work"
    assert view.operations[0].before_day_section.title == "Waiting"
    assert view.operations[0].before_path[-1].title == "Project Atlas"


def test_missing_and_explicitly_absent_day_sections_remain_distinct() -> None:
    value = _hierarchy_plan()
    value["operations"][0]["display"].pop("beforeDaySection")
    value["operations"][2]["display"]["beforeDaySection"] = None
    view = _view(value)
    assert [group.context_state for group in view.previews.before.day_sections] == [
        "none",
        "unknown",
    ]
    assert view.operations[0].before_day_section.state == "unknown"
    assert view.operations[2].before_day_section.state == "none"


def test_split_layout_uses_sections_transitions_and_chronological_pair_order() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    for operation in value["operations"]:
        if operation["action"] == "create":
            operation["display"] = {"afterSection": "Work"}
        elif operation["action"] == "trash":
            operation["display"] = {"beforeSection": "Work"}
        else:
            operation["display"] = {"beforeSection": "Work", "afterSection": "Work"}
    value["operations"][0]["before"]["title"] = "11:00am Wash the dishes"
    value["operations"][0]["after"]["title"] = "3:00pm Wash the dishes"
    value["operations"][1]["after"]["title"] = "2:30pm Get dinner"
    value["operations"][2]["after"]["title"] = "2:00pm Follow up on emails"
    value["operations"][3]["target"]["title"] = "3:30pm Duplicate math task"
    view = _view(value)
    assert len(view.layouts.split) == 1
    assert view.layouts.split[0].operation_ids == (
        "create-email-follow-up",
        "improve-dinner-task",
        "reschedule-wash-dishes",
        "trash-duplicate-math-task",
    )
    assert view.layouts.split[0].counts == {
        "create": 1,
        "update": 2,
        "complete": 0,
        "trash": 1,
    }

    example = _view()
    assert [section.title for section in example.layouts.split] == [
        "Household",
        "Inbox → People",
        "Operations",
        "Math",
    ]


def test_single_pane_layouts_account_for_create_and_trash() -> None:
    view = _view()
    before = {section.title: section.operation_ids for section in view.layouts.before}
    after = {section.title: section.operation_ids for section in view.layouts.after}
    assert before["Created by this plan"] == ("create-email-follow-up",)
    assert after["Pilot-managed Trash"] == ("trash-duplicate-math-task",)
    assert before["Inbox"] == ("improve-dinner-task",)
    assert after["People"] == ("improve-dinner-task",)
    assert sum(len(section.operation_ids) for section in view.layouts.before) == 4
    assert sum(len(section.operation_ids) for section in view.layouts.after) == 4


def test_sparse_cards_distinguish_fallback_clear_and_absent() -> None:
    view = _view()
    schedule = view.operations[0]
    dinner = view.operations[1]
    create = view.operations[2]
    trash = view.operations[3]

    assert schedule.before is not None
    assert schedule.before.title == "Wash the dishes"
    assert schedule.before.title_source == "target"
    assert "unchanged fields omitted" in schedule.before.sparse_label

    estimate = next(diff for diff in dinner.diffs if diff.field == "estimatedTimeDuration")
    assert estimate.before.state == "clear"
    assert estimate.before.exact == "null"
    assert estimate.after.state == "value"
    assert estimate.after.summary == "4h30m"

    create_title = next(diff for diff in create.diffs if diff.field == "title")
    assert create_title.before.state == "absent"
    assert create.before is None
    assert create.before_empty_label == "No before state — created by this plan"
    assert trash.after is None
    assert trash.after_empty_label == "No active after state — deleted with Pilot recovery"


def test_section_fallbacks_and_display_parent_warning() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    operation = value["operations"][1]
    operation["display"] = {"beforeSection": "Review", "afterSection": "People"}
    view = _view(value)
    assert view.operations[1].warnings == (
        "Before display section 'Review' differs from parent title 'Inbox'.",
    )

    del value["operations"][0]["display"]
    view = _view(value)
    assert view.layouts.split[0].title == "Unsectioned changes"


def test_every_task_field_has_an_exact_diff_and_card_fallback() -> None:
    before = {
        "title": "Old title",
        "parent": {"id": "old-parent", "title": "Old parent"},
        "scheduledDate": "2026-08-09",
        "dueDate": "2026-08-10",
        "startDate": "2026-08-09",
        "endDate": "2026-08-11",
        "plannedWeek": "2026-08-10",
        "plannedMonth": "2026-08",
        "labels": [{"id": "old-label", "title": "Old label"}],
        "estimatedTimeDuration": "20m",
        "note": "Old note",
        "subtasks": [
            {"id": "sub-a", "title": "First step", "done": False},
            {"id": "sub-b", "title": "Second step", "done": False},
        ],
        "dayRank": 1,
        "masterRank": 2,
        "dailySection": "Morning",
        "bonusSection": "Essential",
        "customSectionId": "custom-a",
        "timeBlockSectionId": "time-a",
        "starPriority": "yellow",
        "frogSize": "normal",
        "backburner": False,
        "reviewDate": "2026-08-10",
        "snoozedUntil": "2026-08-09T09:00:00-07:00",
        "permanentSnoozeUntil": "09:00",
        "dependencies": ["task-a"],
    }
    after = {
        "title": "New title",
        "parent": {"id": "new-parent", "title": "New parent"},
        "scheduledDate": "2026-08-10",
        "dueDate": "2026-08-11",
        "startDate": "2026-08-10",
        "endDate": "2026-08-12",
        "plannedWeek": "2026-08-17",
        "plannedMonth": "2026-09",
        "labels": [{"id": "new-label", "title": "New label"}],
        "estimatedTimeDuration": "30m",
        "note": "New note",
        "subtasks": [
            {"id": "sub-b", "title": "Second step", "done": True},
            {"id": "sub-a", "title": "Renamed first step", "done": False},
            {"id": "sub-c", "title": "Third step", "done": False},
        ],
        "dayRank": 3,
        "masterRank": 4,
        "dailySection": "Evening",
        "bonusSection": "Bonus",
        "customSectionId": "custom-b",
        "timeBlockSectionId": "time-b",
        "starPriority": "red",
        "frogSize": "monster",
        "backburner": True,
        "reviewDate": "2026-08-11",
        "snoozedUntil": "2026-08-10T10:00:00-07:00",
        "permanentSnoozeUntil": "10:00",
        "dependencies": ["task-b"],
    }
    value = {
        "schemaVersion": 1,
        "planId": "11111111-1111-4111-8111-111111111111",
        "createdAt": "2026-08-09T08:00:00-07:00",
        "summary": "Exercise every visualizer field.",
        "operations": [
            {
                "operationId": "all-fields",
                "action": "update",
                "target": {"type": "task", "id": "task-all", "title": "Old title"},
                "reason": "Visualizer field coverage.",
                "before": before,
                "after": after,
            }
        ],
    }
    operation = _view(value).operations[0]
    assert {diff.field for diff in operation.diffs} == set(TaskFields.model_fields)
    assert operation.before is not None
    assert operation.after is not None
    assert operation.before.fields_shown == len(TaskFields.model_fields)
    assert operation.after.fields_shown == len(TaskFields.model_fields)
    assert [item.id for item in operation.before.subtasks] == ["sub-a", "sub-b"]
    assert [item.id for item in operation.after.subtasks] == ["sub-b", "sub-a", "sub-c"]
    assert operation.after.subtasks[0].done is True


def test_untrusted_html_stays_plain_view_data() -> None:
    value = copy.deepcopy(EXAMPLE_PLAN)
    value["operations"][0]["target"]["title"] = '<img src=x onerror="alert(1)">'
    value["operations"][0]["reason"] = "<script>alert(1)</script>"
    view = _view(value)
    assert view.operations[0].before is not None
    assert view.operations[0].before.title == '<img src=x onerror="alert(1)">'
    assert view.operations[0].reason == "<script>alert(1)</script>"


def test_five_hundred_operation_view_is_complete_and_deterministic() -> None:
    operations = []
    for index in range(500):
        operations.append(
            {
                "operationId": f"create-{index}",
                "action": "create",
                "target": {"type": "task", "id": str(UUID(int=index + 1))},
                "reason": "Scale coverage.",
                "display": {"afterSection": f"Section {index % 5}"},
                "after": {"title": f"Task {index}"},
            }
        )
    value = {
        "schemaVersion": 1,
        "planId": "22222222-2222-4222-8222-222222222222",
        "createdAt": "2026-08-09T08:00:00-07:00",
        "summary": "Scale plan.",
        "operations": operations,
    }
    first = _view(value)
    second = _view(value)
    assert first == second
    assert first.total_operations == 500
    assert len(first.layouts.split) == 5
    assert sum(len(section.operation_ids) for section in first.layouts.split) == 500
