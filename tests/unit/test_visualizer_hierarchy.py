from __future__ import annotations

from marvin_pilot.visualizer_hierarchy import build_backup_hierarchy_context


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
