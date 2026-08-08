from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import marvin_pilot.cli as cli_module
import marvin_pilot.config as config_module
from marvin_pilot.cli import app
from marvin_pilot.examples import EXAMPLE_PLAN

runner = CliRunner()


@pytest.fixture
def isolated_app_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_path = tmp_path / "roaming" / "marvin-pilot" / "config.toml"
    history_path = tmp_path / "local" / "marvin-pilot" / "history"
    monkeypatch.setattr(cli_module, "default_config_path", lambda: config_path)
    monkeypatch.setattr(cli_module, "default_history_dir", lambda: history_path)
    monkeypatch.setattr(cli_module, "load_config", lambda: config_module.load_config(config_path))
    monkeypatch.setattr(
        cli_module, "save_config", lambda config: config_module.save_config(config, config_path)
    )
    monkeypatch.setattr(
        cli_module,
        "effective_history_dir",
        lambda config: (
            Path(config.history_dir).expanduser() if config.history_dir else history_path
        ),
    )
    return tmp_path


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


def test_config_paths_and_show_are_non_secret(isolated_app_dirs: Path) -> None:
    paths = runner.invoke(app, ["config", "paths"])
    show = runner.invoke(app, ["config", "show"])
    assert paths.exit_code == 0
    assert show.exit_code == 0
    assert "marvin-pilot" in paths.stdout
    assert "Credential mode: keyring" in show.stdout
    assert "token" not in show.stdout.lower()


def test_config_mode_commands_round_trip(isolated_app_dirs: Path) -> None:
    key_file = isolated_app_dirs / "key.txt"
    file_mode = runner.invoke(
        app,
        ["config", "set-credential-mode", "file", "--key-file", str(key_file)],
    )
    show = runner.invoke(app, ["config", "show"])
    prompt_mode = runner.invoke(app, ["config", "set-credential-mode", "prompt"])
    assert file_mode.exit_code == 0
    assert "Credential mode: file" in show.stdout
    assert prompt_mode.exit_code == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["config", "set-credential-mode", "wrong"],
        ["config", "set-credential-mode", "file"],
        ["config", "set-credential-mode", "prompt", "--key-file", "token.txt"],
    ],
)
def test_invalid_config_mode_commands_fail(isolated_app_dirs: Path, arguments: list[str]) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 2


def test_config_history_directory_round_trip(isolated_app_dirs: Path) -> None:
    history = isolated_app_dirs / "my-history"
    result = runner.invoke(app, ["config", "set-history-dir", str(history)])
    paths = runner.invoke(app, ["config", "paths"])
    assert result.exit_code == 0
    assert str(history) in paths.stdout


def test_config_token_command_never_echoes_secret(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(cli_module, "store_keyring_token", captured.append)
    result = runner.invoke(app, ["config", "set-full-access-token"], input="very-secret\n")
    assert result.exit_code == 0
    assert captured == ["very-secret"]
    assert "very-secret" not in result.stdout


def test_config_token_requires_keyring_mode(isolated_app_dirs: Path) -> None:
    runner.invoke(app, ["config", "set-credential-mode", "prompt"])
    result = runner.invoke(app, ["config", "set-full-access-token"])
    assert result.exit_code == 2
    assert "requires keyring mode" in result.stderr


def test_config_unset_calls_keyring_without_secret_output(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(cli_module, "delete_keyring_token", lambda: calls.append(True))
    result = runner.invoke(app, ["config", "unset-full-access-token"])
    assert result.exit_code == 0
    assert calls == [True]
    assert "Removed" in result.stdout


def test_guided_config_can_select_prompt_mode(isolated_app_dirs: Path) -> None:
    result = runner.invoke(app, ["config"], input="prompt\n\n")
    assert result.exit_code == 0
    assert "Saved non-secret configuration" in result.stdout
    show = runner.invoke(app, ["config", "show"])
    assert "Credential mode: prompt" in show.stdout
