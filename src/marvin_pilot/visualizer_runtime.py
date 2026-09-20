"""Discover a still-running visualizer for the same local input set."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from platformdirs import user_cache_dir


def session_key(
    plan: str | None,
    backup: Path | None,
    context_plans: list[Path],
    receipt: Path | None,
    allow_apply: bool,
) -> str | None:
    """Return a path-only key; stdin and chooser sessions are not reusable."""

    if plan is None or plan == "-":
        return None
    paths = [str(Path(plan).resolve()), str(backup.resolve()) if backup else None]
    paths.extend(str(path.resolve()) for path in context_plans)
    paths.extend((str(receipt.resolve()) if receipt else None, allow_apply))
    return hashlib.sha256(json.dumps(paths, separators=(",", ":")).encode()).hexdigest()


def registry_dir() -> Path:
    return Path(user_cache_dir("marvin-pilot")) / "visualizer-sessions"


def find_reusable_server(key: str, *, directory: Path | None = None) -> str | None:
    """Trust only a loopback URL whose live server echoes the requested key."""

    root = directory or registry_dir()
    if not root.is_dir():
        return None
    with httpx.Client(timeout=0.5, trust_env=False) as client:
        for entry in root.glob("session-*.json"):
            try:
                record = json.loads(entry.read_text(encoding="utf-8"))
                if record.get("key") != key:
                    continue
                url = record["url"]
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "http"
                    or parsed.hostname != "127.0.0.1"
                    or parsed.port is None
                    or not parsed.path.startswith("/")
                    or parsed.path.count("/") != 2
                    or not parsed.path.endswith("/")
                ):
                    continue
                response = client.get(url + "api/status")
                if response.status_code == 200 and response.json().get("session_key") == key:
                    return url
            except (OSError, ValueError, KeyError, httpx.HTTPError):
                continue
    return None


def register_server(url: str, key: str, *, directory: Path | None = None) -> Path:
    root = directory or registry_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"session-{os.getpid()}.json"
    path.write_text(json.dumps({"url": url, "key": key}), encoding="utf-8")
    return path


def unregister_server(path: Path) -> None:
    path.unlink(missing_ok=True)
