"""Parsing and formatting for explicit estimated-time durations."""

from __future__ import annotations

import re

from marvin_pilot.errors import PlanSyntaxError

_DURATION_RE = re.compile(r"(?:(?P<hours>0|[1-9]\d*)h)?(?:(?P<minutes>0|[1-9]\d*)m)?\Z")


def parse_duration_minutes(value: str) -> int:
    """Parse ``30m``, ``2h``, or ``1h30m`` into positive whole minutes."""

    match = _DURATION_RE.fullmatch(value)
    if match is None or (match.group("hours") is None and match.group("minutes") is None):
        raise PlanSyntaxError(
            f"invalid estimatedTimeDuration {value!r}; use forms such as '30m', '2h', or '1h30m'"
        )
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    total = hours * 60 + minutes
    if total <= 0:
        raise PlanSyntaxError("estimatedTimeDuration must be greater than zero")
    return total


def duration_to_milliseconds(value: str) -> int:
    """Compile a plan duration to Marvin's millisecond ``timeEstimate`` value."""

    return parse_duration_minutes(value) * 60_000


def format_duration_minutes(minutes: int) -> str:
    """Render a positive minute count in canonical plan form."""

    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        raise ValueError("minutes must be a positive integer")
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h{remainder}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"


def normalize_duration(value: str) -> str:
    """Validate a plan duration and return its canonical spelling."""

    return format_duration_minutes(parse_duration_minutes(value))


def milliseconds_to_duration(value: int) -> str:
    """Normalize a positive whole-minute Marvin estimate to plan form."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Marvin timeEstimate must be a positive integer")
    if value % 60_000:
        raise ValueError("Marvin timeEstimate is not a whole number of minutes")
    return format_duration_minutes(value // 60_000)
