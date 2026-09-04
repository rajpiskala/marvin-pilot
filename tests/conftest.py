from __future__ import annotations

import copy
from pathlib import Path
from uuid import uuid4

import pytest

import marvin_pilot.backup_cache as backup_cache_module
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


@pytest.fixture(autouse=True)
def isolated_backup_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test read or mutate the user's real parsed-backup cache."""

    monkeypatch.setattr(backup_cache_module, "backup_cache_dir", lambda: tmp_path / "backup-cache")
