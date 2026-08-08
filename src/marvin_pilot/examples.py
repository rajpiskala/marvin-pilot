"""Canonical examples included in CLI help and tests."""

from __future__ import annotations

EXAMPLE_PLAN = {
    "schemaVersion": 1,
    "planId": "5f97946d-35f3-4a52-84de-4fe51e694788",
    "createdAt": "2026-08-08T14:45:00-07:00",
    "summary": "Refocus today on math and make the dinner task concrete.",
    "operations": [
        {
            "operationId": "reschedule-wash-dishes",
            "action": "update",
            "target": {
                "type": "task",
                "id": "task-wash-dishes-id",
                "title": "Wash the dishes",
            },
            "reason": "Move this non-math task out of today's plan.",
            "before": {"scheduledDate": "2026-08-08"},
            "after": {"scheduledDate": "2026-08-09"},
        },
        {
            "operationId": "improve-dinner-task",
            "action": "update",
            "target": {
                "type": "task",
                "id": "task-dinner-id",
                "title": "Eat dinner with Jacob",
            },
            "reason": "Record the agreed time, place, attendees, and expected length.",
            "before": {
                "title": "Eat dinner with Jacob",
                "estimatedTimeDuration": None,
                "parent": {"id": "unassigned", "title": "Inbox"},
            },
            "after": {
                "title": "5:00pm Get deep dish pizza at Pizzeria Uno with Jacob and Lynn",
                "estimatedTimeDuration": "4h30m",
                "parent": {"id": "people-category-id", "title": "People"},
            },
        },
        {
            "operationId": "create-email-follow-up",
            "action": "create",
            "target": {
                "type": "task",
                "id": "40d06376-9125-4e9e-a6bd-631cb0e6dc55",
            },
            "reason": "The requested email-follow-up block has no existing task.",
            "after": {
                "title": "1:00pm Follow up on emails and respond to people",
                "scheduledDate": "2026-08-08",
                "estimatedTimeDuration": "1h",
                "parent": {"id": "unassigned", "title": "Inbox"},
            },
        },
        {
            "operationId": "trash-duplicate-math-task",
            "action": "trash",
            "target": {
                "type": "task",
                "id": "duplicate-task-id",
                "title": "Study chapter 3",
            },
            "reason": "Duplicate of the more detailed chapter 3 task.",
            "expectedUpdatedAt": 1786221000123,
        },
    ],
}
