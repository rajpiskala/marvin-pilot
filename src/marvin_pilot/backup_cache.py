"""Private content-addressed cache for parsed Marvin backup documents."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs

from marvin_pilot.atomic import atomic_write_bytes, ensure_private_directory
from marvin_pilot.backup_context import MAX_BACKUP_FILE_BYTES, load_backup_documents
from marvin_pilot.errors import HistoryError, PlanSyntaxError

CACHE_VERSION = 1
MAX_CACHE_ENTRIES = 5


def backup_cache_dir() -> Path:
    return Path(PlatformDirs("marvin-pilot", appauthor=False).user_cache_dir) / "backups"


def backup_sha256(path: Path) -> tuple[str, int]:
    try:
        if not path.is_file():
            raise PlanSyntaxError(f"backup path is not a regular file: {path}")
        size = path.stat().st_size
        if size > MAX_BACKUP_FILE_BYTES:
            raise PlanSyntaxError(
                f"Marvin backup exceeds the {MAX_BACKUP_FILE_BYTES}-byte input safety limit"
            )
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except PlanSyntaxError:
        raise
    except OSError as exc:
        raise PlanSyntaxError(f"could not hash Marvin backup {path}: {exc}") from exc
    return digest.hexdigest(), size


def _cache_path(digest: str) -> Path:
    return backup_cache_dir() / f"v{CACHE_VERSION}-{digest}.json"


def _cleanup_lru(directory: Path) -> None:
    paths = sorted(
        directory.glob(f"v{CACHE_VERSION}-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths[MAX_CACHE_ENTRIES:]:
        with suppress(OSError):
            path.unlink()


def load_cached_backup_documents(
    path: Path,
) -> tuple[list[dict[str, Any]], str, int, str, bool]:
    """Load by content hash; return documents, format, bytes, SHA-256, cache hit."""

    digest, size = backup_sha256(path)
    directory = backup_cache_dir()
    try:
        ensure_private_directory(directory)
    except HistoryError:
        documents, source_format, _source_bytes = load_backup_documents(path)
        return documents, source_format, size, digest, False
    cached = _cache_path(digest)
    if cached.is_file():
        try:
            value = json.loads(cached.read_text(encoding="utf-8"))
            if (
                value.get("cacheVersion") == CACHE_VERSION
                and value.get("sha256") == digest
                and isinstance(value.get("documents"), list)
            ):
                cached.touch()
                return value["documents"], value["sourceFormat"], size, digest, True
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    documents, source_format, _source_bytes = load_backup_documents(path)
    payload = {
        "cacheVersion": CACHE_VERSION,
        "sha256": digest,
        "sourceFormat": source_format,
        "sourceBytes": size,
        "cachedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "documents": documents,
    }
    try:
        atomic_write_bytes(
            cached,
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(),
        )
    except HistoryError:
        return documents, source_format, size, digest, False
    _cleanup_lru(directory)
    return documents, source_format, size, digest, False


def clear_backup_cache() -> tuple[int, int]:
    """Delete only Pilot-owned content-addressed backup cache files."""

    directory = backup_cache_dir()
    if not directory.exists():
        return 0, 0
    count = 0
    size = 0
    for path in directory.glob(f"v{CACHE_VERSION}-*.json"):
        try:
            size += path.stat().st_size
            path.unlink()
            count += 1
        except OSError as exc:
            raise PlanSyntaxError(f"could not clear backup cache entry {path}: {exc}") from exc
    return count, size
