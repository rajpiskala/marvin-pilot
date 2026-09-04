"""Verified compact live context from documented Amazing Marvin read endpoints."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Any

from marvin_pilot.errors import RemoteError


def _type(document: dict[str, Any]) -> str:
    if document.get("db") == "Tasks":
        return "task"
    if document.get("db") == "Categories":
        return str(document.get("type", "project"))
    return "unknown"


def build_live_today_context(reader: Any, day: date) -> dict[str, Any]:
    """Resolve Marvin's authoritative Today result into compact exact identities."""

    account = reader.check_connection()
    listed = reader.get_today_items(day.isoformat())
    cache: dict[str, dict[str, Any] | None] = {}

    def get(identifier: str) -> dict[str, Any] | None:
        if identifier not in cache:
            cache[identifier] = reader.get_doc(identifier)
        return deepcopy(cache[identifier]) if cache[identifier] is not None else None

    def path_for(parent_id: Any) -> list[dict[str, str]]:
        reverse = []
        seen: set[str] = set()
        while isinstance(parent_id, str) and parent_id not in {"", "root", "unassigned"}:
            if parent_id in seen:
                raise RemoteError(f"Today hierarchy contains a cycle at {parent_id!r}")
            seen.add(parent_id)
            parent = get(parent_id)
            if parent is None:
                reverse.append({"id": parent_id, "type": "unknown", "title": "<missing>"})
                break
            reverse.append(
                {
                    "id": parent_id,
                    "type": _type(parent),
                    "title": str(parent.get("title") or "<untitled>"),
                }
            )
            parent_id = parent.get("parentId")
        return list(reversed(reverse))

    items = []
    for listed_item in listed:
        identifier = listed_item.get("_id")
        if not isinstance(identifier, str):
            continue
        document = get(identifier) or listed_item
        scheduled = document.get("day")
        recurring = document.get("recurring") is True
        if scheduled == day.isoformat():
            today_reason = "scheduled-date"
        elif isinstance(scheduled, str) and scheduled < day.isoformat():
            today_reason = "rollover"
        else:
            today_reason = "strategy-or-auto-schedule"
        item: dict[str, Any] = {
            "id": identifier,
            "type": _type(document),
            "title": str(document.get("title") or "<untitled>"),
            "done": document.get("done") is True,
            "scheduledDate": scheduled,
            "todayReason": today_reason,
            "path": path_for(document.get("parentId")),
            "updatedAt": document.get("updatedAt"),
            "recurrence": {"scope": "occurrence"} if recurring else None,
        }
        if recurring:
            series_id = document.get("recurringTaskId")
            item["recurrence"]["seriesId"] = series_id
            if isinstance(series_id, str):
                series = get(series_id)
                if series is not None:
                    item["recurrence"]["seriesTitle"] = series.get("title")
        items.append(item)
    return {
        "contextVersion": 1,
        "kind": "today-live",
        "date": day.isoformat(),
        "account": {
            "userId": account.account_user_id,
            "email": account.account_email,
        },
        "count": len(items),
        "items": items,
        "accuracyNote": (
            "Items come from Marvin's documented todayItems endpoint; full documents and "
            "ancestors were resolved by stable ID. todayReason names rollover exactly when the "
            "stored day is earlier, and labels other strategy-added items conservatively."
        ),
    }
