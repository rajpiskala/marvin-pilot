from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from marvin_pilot.live_context import build_live_today_context


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
