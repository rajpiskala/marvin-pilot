from __future__ import annotations

import json
from pathlib import Path

import pytest

import marvin_pilot.backup_cache as backup_cache
from marvin_pilot.errors import HistoryError, PlanSyntaxError


def test_content_addressed_cache_reuses_parsed_backup_and_clear_is_scoped(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(backup_cache, "backup_cache_dir", lambda: cache)
    source = tmp_path / "backup.json"
    source.write_text(
        json.dumps([{"_id": "task-1", "db": "Tasks", "title": "Task"}]),
        encoding="utf-8",
    )

    first = backup_cache.load_cached_backup_documents(source)
    second = backup_cache.load_cached_backup_documents(source)

    assert first[3] == second[3]
    assert first[4] is False
    assert second[4] is True
    unrelated = cache / "keep-me.txt"
    unrelated.write_text("private but not a Pilot cache entry", encoding="utf-8")
    count, _size = backup_cache.clear_backup_cache()
    assert count == 1
    assert unrelated.exists()


def test_backup_hash_rejects_missing_and_oversized_inputs(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(PlanSyntaxError, match="not a regular file"):
        backup_cache.backup_sha256(tmp_path / "missing.json")

    source = tmp_path / "backup.json"
    source.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(backup_cache, "MAX_BACKUP_FILE_BYTES", 1)
    with pytest.raises(PlanSyntaxError, match="input safety limit"):
        backup_cache.backup_sha256(source)


def test_cache_falls_back_when_private_storage_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "backup.json"
    source.write_text(
        json.dumps([{"_id": "task-1", "db": "Tasks", "title": "Task"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(backup_cache, "backup_cache_dir", lambda: tmp_path / "cache")

    def unavailable(_path: Path) -> None:
        raise HistoryError("private directory unavailable")

    monkeypatch.setattr(backup_cache, "ensure_private_directory", unavailable)
    documents, source_format, size, digest, hit = backup_cache.load_cached_backup_documents(source)
    assert documents[0]["_id"] == "task-1"
    assert source_format == "marvin-backup-json"
    assert size > 0 and len(digest) == 64
    assert hit is False


def test_corrupt_cache_is_rebuilt_and_write_failure_remains_usable(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(backup_cache, "backup_cache_dir", lambda: cache)
    source = tmp_path / "backup.json"
    source.write_text("[]", encoding="utf-8")
    digest, _size = backup_cache.backup_sha256(source)
    cache.mkdir()
    backup_cache._cache_path(digest).write_text("not-json", encoding="utf-8")

    def cannot_write(_path: Path, _value: bytes) -> None:
        raise HistoryError("write failed")

    monkeypatch.setattr(backup_cache, "atomic_write_bytes", cannot_write)
    documents, _source_format, _size, _digest, hit = backup_cache.load_cached_backup_documents(
        source
    )
    assert documents == []
    assert hit is False


def test_cache_cleanup_keeps_five_newest_entries_and_clear_handles_missing(
    tmp_path: Path, monkeypatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(backup_cache, "backup_cache_dir", lambda: cache)
    for index in range(7):
        path = cache / f"v1-{index}.json"
        path.write_text("{}", encoding="utf-8")
        path.touch()

    backup_cache._cleanup_lru(cache)
    assert len(list(cache.glob("v1-*.json"))) == 5

    for path in cache.glob("v1-*.json"):
        path.unlink()
    cache.rmdir()
    assert backup_cache.clear_backup_cache() == (0, 0)
