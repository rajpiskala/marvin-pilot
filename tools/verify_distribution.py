"""Verify that built distributions contain the complete offline visualizer."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

REQUIRED_SUFFIXES = (
    "marvin_pilot/visualizer.py",
    "marvin_pilot/visualizer_fields.py",
    "marvin_pilot/visualizer_server.py",
    "marvin_pilot/visualizer_assets/__init__.py",
    "marvin_pilot/visualizer_assets/index.html",
    "marvin_pilot/visualizer_assets/styles.css",
    "marvin_pilot/visualizer_assets/app.js",
)
FORBIDDEN_NAMES = {"dev-creds.json", "Project-Task.md"}


def archive_members(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as archive:
            return set(archive.getnames())
    raise ValueError(f"unsupported distribution format: {path}")


def verify(path: Path) -> None:
    members = archive_members(path)
    missing = [
        suffix
        for suffix in REQUIRED_SUFFIXES
        if not any(member.endswith(suffix) for member in members)
    ]
    forbidden = sorted(member for member in members if Path(member).name in FORBIDDEN_NAMES)
    if missing or forbidden:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if forbidden:
            details.append(f"forbidden: {', '.join(forbidden)}")
        raise SystemExit(f"{path}: {'; '.join(details)}")
    print(f"verified {path}: {len(members)} files")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("distributions", type=Path, nargs="+")
    args = parser.parse_args()
    for distribution in args.distributions:
        verify(distribution)


if __name__ == "__main__":
    main()
