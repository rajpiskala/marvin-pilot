"""Small, durable filesystem primitives for config and audit files."""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path

from marvin_pilot.errors import HistoryError

WINDOWS_REPLACE_ATTEMPTS = 6
WINDOWS_REPLACE_INITIAL_DELAY_SECONDS = 0.05


def _is_retryable_replace_error(error: OSError) -> bool:
    return os.name == "nt" and (
        isinstance(error, PermissionError) or getattr(error, "winerror", None) in {5, 32}
    )


def replace_with_retry(source: Path, target: Path) -> None:
    """Tolerate brief Windows scanner/indexer locks without weakening atomic replace."""

    for attempt in range(WINDOWS_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not _is_retryable_replace_error(exc) or attempt + 1 == WINDOWS_REPLACE_ATTEMPTS:
                raise
            time.sleep(min(0.8, WINDOWS_REPLACE_INITIAL_DELAY_SECONDS * (2**attempt)))


def ensure_private_directory(path: Path) -> None:
    """Create an application directory and restrict Unix permissions where supported."""

    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)
    except OSError as exc:
        raise HistoryError(f"could not create private directory {path}: {exc}") from exc


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Flush a neighboring temporary file and atomically replace ``path``."""

    ensure_private_directory(path.parent)
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(raw_temporary_path)
        if os.name != "nt":
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        replace_with_retry(temporary_path, path)
        temporary_path = None
        if os.name != "nt":
            path.chmod(0o600)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except OSError as exc:
        raise HistoryError(f"could not atomically write {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def exclusive_write_bytes(path: Path, content: bytes) -> None:
    """Durably create ``path`` while refusing to replace an existing file."""

    ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if os.name != "nt":
            path.chmod(0o600)
    except FileExistsError:
        raise
    except OSError as exc:
        raise HistoryError(f"could not create {path}: {exc}") from exc
