from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marvin_pilot.duration import (
    duration_to_milliseconds,
    format_duration_minutes,
    milliseconds_to_duration,
    normalize_duration,
    parse_duration_minutes,
)
from marvin_pilot.errors import PlanSyntaxError


@pytest.mark.parametrize(
    ("value", "minutes", "canonical"),
    [
        ("1m", 1, "1m"),
        ("30m", 30, "30m"),
        ("2h", 120, "2h"),
        ("1h30m", 90, "1h30m"),
        ("60m", 60, "1h"),
        ("2h0m", 120, "2h"),
    ],
)
def test_duration_parsing(value: str, minutes: int, canonical: str) -> None:
    assert parse_duration_minutes(value) == minutes
    assert normalize_duration(value) == canonical
    assert duration_to_milliseconds(value) == minutes * 60_000


@pytest.mark.parametrize(
    "value",
    ["", "0m", "0h", "none", "30", "1.5h", "1h-2m", "01h", " 30m", "30m "],
)
def test_invalid_durations_are_rejected(value: str) -> None:
    with pytest.raises(PlanSyntaxError):
        parse_duration_minutes(value)


@given(st.integers(min_value=1, max_value=10_000_000))
def test_duration_round_trip(minutes: int) -> None:
    rendered = format_duration_minutes(minutes)
    assert parse_duration_minutes(rendered) == minutes
    assert milliseconds_to_duration(minutes * 60_000) == rendered


@pytest.mark.parametrize("value", [0, -1, True, 60_001, 1.5, "60000"])
def test_invalid_marvin_estimates_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        milliseconds_to_duration(value)  # type: ignore[arg-type]
