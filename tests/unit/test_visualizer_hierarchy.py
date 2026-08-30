from __future__ import annotations

from marvin_pilot.visualizer_hierarchy import (
    build_backup_hierarchy_context,
    merge_hierarchy_contexts,
)


def test_backup_hierarchy_keeps_safe_active_metadata_and_latest_duplicate() -> None:
    documents = [
        {
            "_id": "work",
            "db": "Categories",
            "type": "category",
            "title": "Old Work",
            "parentId": "root",
            "updatedAt": 1,
        },
        {
            "_id": "work",
            "db": "Categories",
            "type": "category",
            "title": "Work",
            "parentId": "root",
            "emoji": "💼",
            "color": "#C69C7B",
            "rank": 7,
            "updatedAt": 2,
        },
        {
            "_id": "task",
            "db": "Tasks",
            "title": "Nested task",
            "parentId": "work",
        },
        {
            "_id": "trashed",
            "db": "Tasks",
            "title": "Trashed task",
            "deletedAt": 123,
        },
        {"_id": "profile", "db": "ProfileItems", "title": "Ignore me"},
    ]

    context = build_backup_hierarchy_context(documents)

    assert context.input_document_count == 5
    assert set(context.nodes) == {"work", "task"}
    work = context.nodes["work"]
    assert (work.title, work.parent_id, work.emoji, work.color, work.order) == (
        "Work",
        "root",
        "💼",
        "#c69c7b",
        7,
    )
    assert context.nodes["task"].type == "task"


def test_backup_hierarchy_discards_invalid_optional_color() -> None:
    context = build_backup_hierarchy_context(
        [
            {
                "_id": "project",
                "db": "Categories",
                "type": "project",
                "title": "Project",
                "color": "red; background: url(evil)",
            }
        ]
    )
    assert context.nodes["project"].color is None


def test_receipt_context_can_override_a_newer_post_apply_backup() -> None:
    backup = build_backup_hierarchy_context(
        [
            {
                "_id": "task",
                "db": "Tasks",
                "title": "Deleted task",
                "parentId": "new-parent",
                "deletedAt": 200,
                "updatedAt": 200,
            }
        ]
    )
    receipt = build_backup_hierarchy_context(
        [
            {
                "_id": "task",
                "db": "Tasks",
                "title": "Deleted task",
                "parentId": "original-parent",
                "updatedAt": 100,
            }
        ]
    )

    merged = merge_hierarchy_contexts(backup, receipt)

    assert merged is not None
    assert merged.nodes["task"].parent_id == "original-parent"
    assert merged.input_document_count == 2
