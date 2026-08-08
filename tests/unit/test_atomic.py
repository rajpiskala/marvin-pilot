from __future__ import annotations

from pathlib import Path

import pytest

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
