from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

import pytest

from marvin_pilot.examples import EXAMPLE_PLAN


def pytest_configure(config: pytest.Config) -> None:
    """Give every sandbox process a fresh temp root it never needs to pre-clean."""

    if config.option.basetemp is None:
        test_runs = Path.cwd() / ".test-runs"
        test_runs.mkdir(exist_ok=True)
        config.option.basetemp = test_runs / f"pytest-{uuid4()}"


@pytest.fixture
def example_plan_dict() -> dict:
    return copy.deepcopy(EXAMPLE_PLAN)
