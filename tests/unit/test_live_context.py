from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from marvin_pilot.errors import PlanSyntaxError, RemoteError
from marvin_pilot.live_context import build_live_search_context, build_live_today_context


class Reader:
    def __init__(self) -> None:
        self.documents = {
            "task-1": {
                "_id": "task-1",
                "db": "Tasks",
                "title": "Rolled task",
                "day": "2026-09-03",
                "parentId": "project-1",
                "recurring": True,
                "recurringTaskId": "series-1",
                "updatedAt": 10,
            },
            "project-1": {
                "_id": "project-1",
                "db": "Categories",
                "type": "project",
                "title": "Project",
                "parentId": "category-1",
            },
            "category-1": {
                "_id": "category-1",
                "db": "Categories",
                "type": "category",
                "title": "Category",
                "parentId": "root",
            },
            "series-1": {
                "_id": "series-1",
                "db": "RecurringTasks",
                "recurringType": "task",
                "title": "Series",
            },
        }

    def check_connection(self):
        return SimpleNamespace(account_user_id="123456", account_email="pilot@example.com")

    def get_today_items(self, day: str):
        assert day == "2026-09-04"
        return [{"_id": "task-1", "title": "Rolled task"}]

    def get_doc(self, item_id: str):
        return self.documents.get(item_id)


def test_today_context_uses_authoritative_endpoint_and_classifies_rollover() -> None:
    context = build_live_today_context(Reader(), date(2026, 9, 4))
    assert context["account"]["userId"] == "123456"
    assert context["items"][0]["todayReason"] == "rollover"
    assert [node["title"] for node in context["items"][0]["path"]] == [
        "Category",
        "Project",
    ]
    assert context["items"][0]["recurrence"]["seriesTitle"] == "Series"


class SearchReader:
    def __init__(self, documents):
        self.documents = documents
        self.calls = []

    def check_connection(self):
        self.calls.append("account")
        return SimpleNamespace(account_user_id="123456", account_email="pilot@example.com")

    def get_categories(self):
        self.calls.append("categories")
        return self.documents


def test_live_search_handles_nested_categories_duplicate_titles_and_missing_ancestors():
    documents = [
        {"_id": "area", "type": "category", "title": "Area", "parentId": "root"},
        {"_id": "one", "type": "project", "title": "Launch", "parentId": "area", "done": True},
        {"_id": "two", "type": "project", "title": "Launch", "parentId": "missing"},
    ]
    reader = SearchReader(documents)
    result = build_live_search_context(reader, "launch")
    assert reader.calls == ["account", "categories"]
    assert result["ambiguous"] and result["count"] == 2
    assert result["candidates"][0]["done"] is True
    assert [node["id"] for node in result["candidates"][0]["path"]] == ["area", "one"]
    assert result["candidates"][1]["hierarchyWarnings"] == [
        {"kind": "missing-parent", "id": "missing"}
    ]
    assert "db" not in documents[0]


@pytest.mark.parametrize("parent", ["cycle", []])
def test_live_search_reports_bad_hierarchy_without_lookups(parent):
    reader = SearchReader(
        [{"_id": "cycle", "type": "project", "title": "Example", "parentId": parent}]
    )
    result = build_live_search_context(reader, "example")
    assert result["candidates"][0]["hierarchyComplete"] is False


def test_live_search_zero_results_normalization_and_truncation():
    reader = SearchReader(
        [
            {"_id": str(i), "type": "project", "title": "📁 Example", "parentId": "unassigned"}
            for i in range(101)
        ]
    )
    result = build_live_search_context(reader, "example")
    assert result["count"] == 101 and len(result["candidates"]) == 100 and result["truncated"]
    assert build_live_search_context(reader, "absent")["count"] == 0
    with pytest.raises(PlanSyntaxError, match="non-empty"):
        build_live_search_context(reader, "  ")


def test_live_search_rejects_duplicate_collection_ids():
    doc = {"_id": "duplicate", "type": "category", "title": "Area"}
    with pytest.raises(RemoteError, match="duplicate"):
        build_live_search_context(SearchReader([doc, doc]), "area")


def test_live_search_warns_when_server_collection_may_be_capped():
    docs = [{"_id": str(i), "type": "project", "title": "Example"} for i in range(200)]
    result = build_live_search_context(SearchReader(docs), "not-found")
    assert result["count"] == 0
    assert result["collectionCount"] == 200
    assert result["collectionMayBeTruncated"] is True
    assert result["warnings"][0]["code"] == "POSSIBLY_TRUNCATED_COLLECTION"
