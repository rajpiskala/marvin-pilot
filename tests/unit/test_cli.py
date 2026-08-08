from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from marvin_pilot.cli import app
from marvin_pilot.examples import EXAMPLE_PLAN

runner = CliRunner()


def write_plan(path: Path, plan: dict = EXAMPLE_PLAN) -> None:
    path.write_text(json.dumps(plan), encoding="utf-8")


def test_main_help_leads_with_safety_contract() -> None:
    result = runner.invoke(app, ["--help"])
    normalized = " ".join(result.stdout.split())
    assert result.exit_code == 0
    assert "separates AI-authored proposals" in normalized
    assert "Do not invoke apply or revert" in normalized


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "marvin-pilot 0.1.0.dev0"


def test_example_pipes_into_validate() -> None:
    example = runner.invoke(app, ["example"])
    result = runner.invoke(app, ["validate", "-"], input=example.stdout)
    assert example.exit_code == 0
    assert result.exit_code == 0
    assert "Valid Marvin Pilot change plan: 4 operation(s)" in result.stdout
    assert "Digest: sha256:" in result.stdout


def test_validate_schema_failure_uses_exit_code_2(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text("[]", encoding="utf-8")
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 2
    assert "plan root must be a JSON object" in result.stderr


def test_validate_semantic_failure_uses_exit_code_3(tmp_path: Path) -> None:
    plan = json.loads(json.dumps(EXAMPLE_PLAN))
    plan["operations"][0]["after"] = dict(plan["operations"][0]["before"])
    path = tmp_path / "invalid.json"
    write_plan(path, plan)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 3
    assert "does not change any values" in result.stderr


def test_describe_is_offline_and_readable(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    write_plan(path)
    result = runner.invoke(app, ["describe", str(path)])
    assert result.exit_code == 0
    assert 'UPDATE "Wash the dishes"' in result.stdout
    assert "Totals: 1 create, 2 updates, 1 trash" in result.stdout


def test_live_describe_is_safely_disabled_until_preflight_lands(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    write_plan(path)
    result = runner.invoke(app, ["describe", str(path), "--live"])
    assert result.exit_code == 2
    assert "credentialed preflight milestone" in result.stderr


def test_schema_command_outputs_json() -> None:
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["title"] == "Marvin Pilot change plan v1"


def test_output_commands_refuse_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "example.json"
    path.write_text("keep me", encoding="utf-8")
    result = runner.invoke(app, ["example", "--output", str(path)])
    assert result.exit_code == 2
    assert path.read_text(encoding="utf-8") == "keep me"


def test_output_commands_create_new_files(tmp_path: Path) -> None:
    example_path = tmp_path / "example.json"
    schema_path = tmp_path / "schema.json"
    example = runner.invoke(app, ["example", "--output", str(example_path)])
    schema = runner.invoke(app, ["schema", "--output", str(schema_path)])
    assert example.exit_code == 0
    assert schema.exit_code == 0
    assert json.loads(example_path.read_text(encoding="utf-8"))["schemaVersion"] == 1
    assert json.loads(schema_path.read_text(encoding="utf-8"))["title"].startswith("Marvin Pilot")


def test_missing_plan_path_is_an_actionable_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", str(tmp_path / "missing.json")])
    assert result.exit_code == 2
    assert "not a regular file" in result.stderr


def test_plan_format_help_is_llm_complete() -> None:
    result = runner.invoke(app, ["help", "plan-format"])
    assert result.exit_code == 0
    assert "estimatedTimeDuration" in result.stdout
    assert "scheduledDate: null" in result.stdout
    assert "comments" in result.stdout
    assert "permanent deletion is unsupported" in result.stdout
    assert "--only op-a --only op-b" in result.stdout
