from __future__ import annotations

import copy

import pytest

from marvin_pilot.examples import EXAMPLE_PLAN


@pytest.fixture
def example_plan_dict() -> dict:
    return copy.deepcopy(EXAMPLE_PLAN)
