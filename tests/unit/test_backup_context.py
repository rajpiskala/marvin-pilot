from __future__ import annotations

import json
import lzma
from pathlib import Path

import pytest

import marvin_pilot.backup_context as backup_context
from marvin_pilot.backup_context import (
    build_project_context,
    load_backup_documents,
    project_context_json,
)
from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError

DONE_AT = 1_784_856_600_000


def backup_documents() -> list[dict]:
    return [
        {
            "_id": "work",
            "db": "Categories",
            "type": "category",
            "title": "Work",
            "parentId": "unassigned",
            "rank": 1,
        },
        {
            "_id": "alpha",
            "db": "Categories",
            "type": "project",
            "title": "Project Alpha",
            "parentId": "work",
            "rank": 1,
            "updatedAt": 100,
        },
        {
            "_id": "migration",
            "db": "Categories",
            "type": "project",
            "title": "Migration",
            "parentId": "alpha",
            "done": True,
            "doneDate": "2026-07-20",
            "rank": 1,
        },
        {
            "_id": "open-task",
            "db": "Tasks",
            "title": "Verify the migration",
            "parentId": "migration",
            "done": False,
            "rank": 1,
        },
        {
            "_id": "done-task",
            "db": "Tasks",
            "title": "Write release notes",
            "parentId": "alpha",
            "done": True,
            "doneAt": DONE_AT,
            "updatedAt": 200,
            "note": "Capture the decisions that shipped.",
            "subtasks": {
                "second": {"_id": "second", "title": "Publish", "rank": 2, "done": True},
                "first": {"_id": "first", "title": "Draft", "rank": 1, "done": False},
            },
            "rank": 2,
        },
        {
            "_id": "child-task",
            "db": "Tasks",
            "title": "Archive supporting material",
            "parentId": "done-task",
            "done": False,
            "rank": 1,
        },
        {
            "_id": "series",
            "db": "RecurringTasks",
            "recurringType": "task",
            "type": "weekly",
            "title": "Weekly release review",
            "parentId": "alpha",
            "repeatStart": "2026-01-01",
            "rank": 3,
        },
        {
            "_id": "occurrence",
            "db": "Tasks",
            "title": "Weekly release review",
            "parentId": "alpha",
            "done": False,
            "recurring": True,
            "recurringTaskId": "series",
            "day": "2026-08-18",
            "rank": 4,
        },
        {
            "_id": "trashed-project",
            "db": "Categories",
            "type": "project",
            "title": "Obsolete work",
            "parentId": "alpha",
            "deletedAt": 1_787_078_400_000,
            "rank": 5,
        },
        {
            "_id": "hidden-child",
            "db": "Tasks",
            "title": "Child beneath Trash",
            "parentId": "trashed-project",
            "done": False,
        },
        {
            "_id": "unrelated",
            "db": "Tasks",
            "title": "Sibling outside the project",
            "parentId": "work",
            "done": False,
        },
        {"_id": "profile", "db": "ProfileItems", "title": "Ignored"},
    ]


def test_build_project_context_includes_complete_history_and_nested_tasks() -> None:
    context = build_project_context(backup_documents(), "Project Alpha", source_bytes=123)

    assert context["root"]["id"] == "alpha"
    assert [node["title"] for node in context["root"]["path"]] == [
        "Work",
        "Project Alpha",
    ]
    assert context["counts"] == {
        "descendants": 6,
        "category": 0,
        "project": 1,
        "task": 4,
        "recurringTask": 1,
        "open": 4,
        "completed": 2,
        "withSubtasks": 1,
        "trashedIncluded": 0,
        "trashedExcluded": 2,
    }
    assert [item["id"] for item in context["items"]] == [
        "migration",
        "open-task",
        "done-task",
        "child-task",
        "series",
        "occurrence",
    ]
    completed = next(item for item in context["items"] if item["id"] == "done-task")
    assert completed["completedAt"] == "2026-07-24T01:30:00.000Z"
    assert completed["updatedAt"] == 200
    assert [subtask["id"] for subtask in completed["subtasks"]] == ["first", "second"]
    assert completed["subtasks"][1]["done"] is True
    assert "Sibling outside the project" not in json.dumps(context)


def test_include_trash_restores_the_excluded_subtree() -> None:
    context = build_project_context(backup_documents(), "alpha", include_trash=True)
    assert context["counts"]["descendants"] == 8
    assert context["counts"]["trashedIncluded"] == 1
    assert context["counts"]["trashedExcluded"] == 0
    assert {item["id"] for item in context["items"]} >= {
        "trashed-project",
        "hidden-child",
    }


def test_ambiguous_title_reports_paths_and_accepts_an_exact_id() -> None:
    documents = backup_documents()
    documents.extend(
        [
            {
                "_id": "personal",
                "db": "Categories",
                "type": "category",
                "title": "Personal",
                "parentId": "unassigned",
            },
            {
                "_id": "alpha-duplicate",
                "db": "Categories",
                "type": "project",
                "title": "Project Alpha",
                "parentId": "personal",
            },
        ]
    )
    with pytest.raises(PlanSemanticError, match=r"ambiguous.*Work / Project Alpha"):
        build_project_context(documents, "Project Alpha")
    assert build_project_context(documents, "alpha-duplicate")["root"]["id"] == ("alpha-duplicate")


def test_latest_duplicate_document_wins_with_a_warning() -> None:
    documents = backup_documents()
    documents.append(
        {
            "_id": "alpha",
            "db": "Categories",
            "type": "project",
            "title": "Project Alpha renamed",
            "parentId": "work",
            "updatedAt": 300,
        }
    )
    context = build_project_context(documents, "alpha")
    assert context["root"]["title"] == "Project Alpha renamed"
    assert context["warnings"] == ["Resolved 1 duplicate document ID(s) by latest updatedAt."]


def test_plain_and_lzma_backups_support_marvins_cesu8_emoji(tmp_path: Path) -> None:
    documents = backup_documents()
    documents[1]["title"] = "\ud83d\udcd5 Project Alpha"
    raw = json.dumps(documents, ensure_ascii=False).encode("utf-8", errors="surrogatepass")
    plain = tmp_path / "backup.json"
    compressed = tmp_path / "backup.json.lzma"
    plain.write_bytes(raw)
    compressed.write_bytes(lzma.compress(raw))

    for path, expected_format in (
        (plain, "marvin-backup-json"),
        (compressed, "marvin-backup-json-lzma"),
    ):
        loaded, source_format, source_bytes = load_backup_documents(path)
        assert loaded[1]["title"] == "📕 Project Alpha"
        assert source_format == expected_format
        assert source_bytes == path.stat().st_size
        context = json.loads(project_context_json(path, "📕 Project Alpha"))
        assert context["root"]["title"] == "📕 Project Alpha"


def test_context_never_embeds_the_private_backup_path(tmp_path: Path) -> None:
    backup = tmp_path / "private-account-MarvinBackup.json"
    backup.write_text(json.dumps({"docs": backup_documents()}), encoding="utf-8")

    rendered = project_context_json(backup, "Project Alpha")

    assert str(backup) not in rendered
    assert backup.name not in rendered
    assert json.loads(rendered)["source"]["documentCount"] == len(backup_documents())


def test_decompressed_backup_size_limit_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = tmp_path / "backup.json.lzma"
    backup.write_bytes(lzma.compress(json.dumps(backup_documents()).encode()))
    monkeypatch.setattr(backup_context, "MAX_DECOMPRESSED_BACKUP_BYTES", 20)

    with pytest.raises(PlanSyntaxError, match=r"decompressed Marvin backup exceeds"):
        load_backup_documents(backup)


def test_invalid_backup_shapes_fail_without_tracebacks(tmp_path: Path) -> None:
    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{}", encoding="utf-8")
    with pytest.raises(PlanSyntaxError, match=r"root must be a JSON array"):
        load_backup_documents(invalid_json)

    invalid_lzma = tmp_path / "invalid.json.lzma"
    invalid_lzma.write_bytes(b"not lzma")
    with pytest.raises(PlanSyntaxError, match="could not read Marvin backup"):
        load_backup_documents(invalid_lzma)
