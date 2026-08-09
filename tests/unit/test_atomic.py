from __future__ import annotations

from pathlib import Path

import pytest

import marvin_pilot.atomic as atomic
from marvin_pilot.atomic import atomic_write_bytes, exclusive_write_bytes


def test_atomic_write_creates_and_replaces(tmp_path: Path) -> None:
    path = tmp_path / "private" / "value.json"
    atomic_write_bytes(path, b"first")
    assert path.read_bytes() == b"first"
    atomic_write_bytes(path, b"second")
    assert path.read_bytes() == b"second"
    assert not list(path.parent.glob("*.tmp"))


def test_exclusive_write_refuses_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    exclusive_write_bytes(path, b"original")
    with pytest.raises(FileExistsError):
        exclusive_write_bytes(path, b"replacement")
    assert path.read_bytes() == b"original"


def test_atomic_replace_retries_transient_windows_access_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipt.json"
    path.write_bytes(b"old")
    real_replace = atomic.os.replace
    calls = 0
    sleeps: list[float] = []

    def flaky_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(13, "injected transient access denied")
        real_replace(source, target)

    monkeypatch.setattr(atomic.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic, "_is_retryable_replace_error", lambda _error: True)
    monkeypatch.setattr(atomic.time, "sleep", sleeps.append)

    atomic_write_bytes(path, b"new")

    assert path.read_bytes() == b"new"
    assert calls == 3
    assert sleeps == [0.05, 0.1]
    assert not list(tmp_path.glob("*.tmp"))
