"""Deterministic task and project completion calendar semantics."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

CompletionDayBehavior = Literal["assigned", "preserved", "replaced"]


def completion_local_date(completed_at: str) -> str:
    """Return the calendar date carried by an RFC 3339 completion timestamp.

    Change plans require an explicit numeric offset (or ``Z``), so this does not
    depend on the machine or browser timezone used to apply the plan.
    """

    candidate = completed_at[:-1] + "+00:00" if completed_at.endswith("Z") else completed_at
    return datetime.fromisoformat(candidate).date().isoformat()


def completion_fields(completed_at: str, *, project: bool = False) -> dict[str, Any]:
    """Use identical final completion values for existing items and completed creates."""

    candidate = completed_at[:-1] + "+00:00" if completed_at.endswith("Z") else completed_at
    values: dict[str, Any] = {
        "done": True,
        "doneAt": int(datetime.fromisoformat(candidate).timestamp() * 1000),
        "day": completion_local_date(completed_at),
    }
    if project:
        values["doneDate"] = values["day"]
    return values


def usable_marvin_day(value: Any) -> str | None:
    """Normalize Marvin's missing/unassigned day sentinels to ``None``."""

    if not isinstance(value, str) or value in {"", "unassigned"}:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def completion_day_behavior(before_day: Any, after_day: str) -> CompletionDayBehavior:
    """Describe how assigning the completion-history day changes live state."""

    normalized = usable_marvin_day(before_day)
    if normalized is None:
        return "assigned"
    if normalized == after_day:
        return "preserved"
    return "replaced"
