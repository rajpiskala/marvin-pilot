from __future__ import annotations

import pytest

from marvin_pilot.completion import (
    completion_day_behavior,
    completion_local_date,
    usable_marvin_day,
)


@pytest.mark.parametrize(
    ("completed_at", "expected"),
    [
        ("2026-09-04T00:05:00-07:00", "2026-09-04"),
        ("2026-09-04T23:55:00+05:30", "2026-09-04"),
        ("2026-11-01T01:30:00-07:00", "2026-11-01"),
        ("2026-11-01T01:30:00-08:00", "2026-11-01"),
        ("2026-09-05T00:00:00Z", "2026-09-05"),
    ],
)
def test_completion_local_date_uses_the_timestamp_offset(completed_at: str, expected: str) -> None:
    assert completion_local_date(completed_at) == expected


@pytest.mark.parametrize("value", [None, "", "unassigned", 123, "not-a-date"])
def test_usable_marvin_day_normalizes_missing_and_invalid_values(value: object) -> None:
    assert usable_marvin_day(value) is None


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (None, "2026-09-04", "assigned"),
        ("unassigned", "2026-09-04", "assigned"),
        ("2026-09-04", "2026-09-04", "preserved"),
        ("2026-09-01", "2026-09-04", "replaced"),
        ("2026-09-07", "2026-09-04", "replaced"),
    ],
)
def test_completion_day_behavior_covers_native_state_matrix(
    before: object, after: str, expected: str
) -> None:
    assert completion_day_behavior(before, after) == expected
