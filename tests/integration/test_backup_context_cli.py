from __future__ import annotations

import json
import lzma
from pathlib import Path

from typer.testing import CliRunner

from marvin_pilot.cli import app

runner = CliRunner()


def write_backup(path: Path) -> None:
    documents = [
        {
            "_id": "root-project",
            "db": "Categories",
            "type": "project",
            "title": "Synthetic project",
            "parentId": "unassigned",
        },
        {
            "_id": "completed-task",
            "db": "Tasks",
            "title": "Synthetic completed task",
            "parentId": "root-project",
            "done": True,
            "doneAt": 1_784_856_600_000,
            "updatedAt": 123,
        },
    ]
    path.write_bytes(lzma.compress(json.dumps(documents).encode()))


def test_context_project_cli_reads_compressed_backup_without_a_credential(tmp_path: Path) -> None:
    backup = tmp_path / "backup.json.lzma"
    write_backup(backup)

    result = runner.invoke(
        app,
        ["context", "project", "Synthetic project", "--backup", str(backup)],
    )

    assert result.exit_code == 0
    context = json.loads(result.stdout)
    assert context["root"]["id"] == "root-project"
    assert context["counts"]["completed"] == 1
    assert context["items"][0]["completedAt"] == "2026-07-24T01:30:00.000Z"
    assert str(backup) not in result.stdout
    assert backup.name not in result.stdout


def test_context_project_cli_writes_once_and_refuses_overwrite(tmp_path: Path) -> None:
    backup = tmp_path / "backup.json.lzma"
    output = tmp_path / "context.json"
    write_backup(backup)

    first = runner.invoke(
        app,
        [
            "context",
            "project",
            "root-project",
            "--backup",
            str(backup),
            "--output",
            str(output),
        ],
    )
    second = runner.invoke(
        app,
        [
            "context",
            "project",
            "root-project",
            "--backup",
            str(backup),
            "--output",
            str(output),
        ],
    )

    assert first.exit_code == 0
    assert first.stdout.strip() == str(output)
    assert json.loads(output.read_text(encoding="utf-8"))["counts"]["descendants"] == 1
    assert second.exit_code == 2
    assert "refusing to overwrite" in second.stderr
