from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import marvin_pilot.cli as cli_module
import marvin_pilot.config as config_module
from marvin_pilot.cli import app
from marvin_pilot.errors import CredentialError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.field_registry import FIELD_SPECS

runner = CliRunner()


class CliMarvinClient:
    api_base_host = "https://marvin.test"

    def __init__(self, document: dict) -> None:
        self.document = copy.deepcopy(document)
        self.closed = False
        self.mutations = 0

    def get_doc(self, item_id: str):
        if self.document.get("_id") == item_id:
            return copy.deepcopy(self.document)
        return None

    def update_doc(self, item_id: str, setters: list[dict]):
        assert item_id == self.document["_id"]
        for setter in setters:
            key = setter["key"]
            if key.startswith("fieldUpdates."):
                self.document.setdefault("fieldUpdates", {})[key.split(".", 1)[1]] = setter["val"]
            else:
                self.document[key] = setter["val"]
        self.document["_rev"] = "2-updated"
        self.mutations += 1
        return {"ok": True}

    def create_doc(self, document: dict):
        raise AssertionError("not expected in this CLI fixture")

    def close(self) -> None:
        self.closed = True


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


def one_operation_plan() -> dict:
    plan = json.loads(json.dumps(EXAMPLE_PLAN))
    plan["operations"] = [plan["operations"][0]]
    return plan


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


def test_live_describe_requires_a_credential_before_network_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plan.json"
    write_plan(path)
    monkeypatch.setattr(
        cli_module,
        "load_full_access_token",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CredentialError("no full-access token is stored")
        ),
    )
    result = runner.invoke(app, ["describe", str(path), "--live"])
    assert result.exit_code == 4
    assert "no full-access token is stored" in result.stderr


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


def test_contract_test_commands_generate_verify_and_refuse_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "suite"
    arguments = [
        "contract-tests",
        "generate",
        str(output),
        "--base-date",
        "2026-08-10",
        "--run-id",
        "11111111-1111-4111-8111-111111111111",
        "--created-at",
        "2026-08-08T20:00:00-07:00",
        "--scale-count",
        "10",
    ]
    generated = runner.invoke(app, arguments)
    verified = runner.invoke(app, ["contract-tests", "verify", str(output)])
    repeated = runner.invoke(app, arguments)
    assert generated.exit_code == 0
    assert "Verified: 24 valid case(s)" in generated.stdout
    assert "Optional live coverage not configured" in generated.stdout
    assert verified.exit_code == 0
    assert "24 valid case(s)" in verified.stdout
    assert repeated.exit_code == 2
    assert "refusing to overwrite" in repeated.stderr


def test_contract_test_account_schema_and_invalid_date(tmp_path: Path) -> None:
    schema = runner.invoke(app, ["contract-tests", "account-schema"])
    invalid = runner.invoke(
        app,
        ["contract-tests", "generate", str(tmp_path / "suite"), "--base-date", "08/10/2026"],
    )
    assert schema.exit_code == 0
    assert json.loads(schema.stdout)["additionalProperties"] is False
    assert invalid.exit_code == 2
    assert "--base-date must use YYYY-MM-DD" in invalid.stderr


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
    assert all(field in result.stdout for field in FIELD_SPECS)
    example_text = result.stdout.split("COMPLETE VERSION 1 EXAMPLE\n", maxsplit=1)[1]
    assert json.loads(example_text) == EXAMPLE_PLAN


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


def test_live_describe_runs_preflight_with_injected_client(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path, one_operation_plan())
    client = CliMarvinClient(
        {
            "_id": "task-wash-dishes-id",
            "_rev": "1-task",
            "db": "Tasks",
            "title": "Wash the dishes",
            "day": "2026-08-08",
            "firstScheduled": "2026-01-01",
            "updatedAt": 1,
        }
    )
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    result = runner.invoke(app, ["describe", str(path), "--live"])
    assert result.exit_code == 0
    assert "Live preflight: PASSED for 1 operation(s)" in result.stdout
    assert client.closed


def test_apply_and_history_commands_work_end_to_end_with_mocked_marvin(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    key_file = isolated_app_dirs / "token.key"
    write_plan(path, one_operation_plan())
    client = CliMarvinClient(
        {
            "_id": "task-wash-dishes-id",
            "_rev": "1-task",
            "db": "Tasks",
            "title": "Wash the dishes",
            "day": "2026-08-08",
            "firstScheduled": "2026-01-01",
            "updatedAt": 1,
        }
    )
    received_key_files: list[Path | None] = []

    def make_client(_config, received_key_file):
        received_key_files.append(received_key_file)
        return client

    monkeypatch.setattr(cli_module, "_client_from_config", make_client)
    monkeypatch.setattr(cli_module, "confirm_apply", lambda count: count == 1)
    applied = runner.invoke(
        app,
        ["apply", str(path), "--full-access-key-file", str(key_file)],
    )
    assert applied.exit_code == 0
    assert "Live preflight: PASSED" in applied.stdout
    assert "Receipt:" in applied.stdout
    assert "Operation 1/1 applied" in applied.stderr
    assert received_key_files == [key_file]
    assert client.document["day"] == "2026-08-09"
    assert client.closed

    receipt_path = Path(applied.stdout.strip().split("Receipt: ")[-1])
    listed = runner.invoke(app, ["history", "list"])
    shown = runner.invoke(app, ["history", "show", "latest"])
    verified = runner.invoke(app, ["history", "verify", str(receipt_path)])
    history_path = runner.invoke(app, ["history", "path"])
    assert "apply  applied" in listed.stdout
    assert '"status": "applied"' in shown.stdout
    assert "Valid receipt:" in verified.stdout
    assert str(receipt_path.parent) in history_path.stdout

    monkeypatch.setattr(cli_module, "confirm_revert", lambda count: count == 1)
    client.closed = False
    reverted = runner.invoke(
        app,
        [
            "revert",
            str(path),
            "--only",
            "reschedule-wash-dishes",
            "--full-access-key-file",
            str(key_file),
        ],
    )
    assert reverted.exit_code == 0
    assert "Live revert preflight: PASSED" in reverted.stdout
    assert "Operation 1/1 reverted" in reverted.stderr
    assert client.document["day"] == "2026-08-08"
    assert client.closed
    assert received_key_files == [key_file, key_file]
    assert "revert  reverted" in runner.invoke(app, ["history", "list"]).stdout


def test_revert_plan_lookup_requires_an_exact_apply_receipt(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = isolated_app_dirs / "never-applied.json"
    write_plan(plan_path, one_operation_plan())
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("missing receipt must fail before credentials"),
    )
    result = runner.invoke(app, ["revert", str(plan_path)])
    assert result.exit_code == 3
    assert "no applied or partial receipt exactly matches" in result.stderr


def test_apply_decline_has_exit_6_and_no_receipt(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path, one_operation_plan())
    client = CliMarvinClient(
        {
            "_id": "task-wash-dishes-id",
            "db": "Tasks",
            "title": "Wash the dishes",
            "day": "2026-08-08",
        }
    )
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    monkeypatch.setattr(cli_module, "confirm_apply", lambda _count: False)
    result = runner.invoke(app, ["apply", str(path)])
    assert result.exit_code == 6
    assert client.mutations == 0
    assert runner.invoke(app, ["history", "list"]).stdout.strip() == "No receipts."


def test_apply_enforces_configured_operation_limit_before_credentials(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path)
    cli_module.save_config(
        config_module.AppConfig(max_operations=1, large_plan_warning_operations=1)
    )
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("credential/client must not load"),
    )
    result = runner.invoke(app, ["apply", str(path)])
    assert result.exit_code == 3
    assert "configured maximum is 1" in result.stderr


def test_apply_has_no_inline_token_or_noninteractive_bypass(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path, one_operation_plan())
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("invalid options must fail before credentials"),
    )
    inline = runner.invoke(app, ["apply", str(path), "--full-access-key", "secret"])
    bypass = runner.invoke(app, ["apply", str(path), "--yes"])
    help_result = runner.invoke(app, ["apply", "--help"])
    assert inline.exit_code == 2
    assert bypass.exit_code == 2
    assert "--full-access-key-file" in help_result.stdout
    assert "--yes" not in help_result.stdout


def test_revert_accepts_repeated_only_and_has_no_unsafe_bypasses(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = isolated_app_dirs / "missing-receipt.json"
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("receipt/options must fail before credentials"),
    )
    inline = runner.invoke(app, ["revert", str(missing), "--full-access-key", "secret"])
    bypass = runner.invoke(app, ["revert", str(missing), "--yes"])
    help_result = runner.invoke(app, ["revert", "--help"])
    assert inline.exit_code == 2
    assert bypass.exit_code == 2
    assert "--only" in help_result.stdout
    assert "repeat for multiple operations" in " ".join(help_result.stdout.split())
    assert "--full-access-key-file" in help_result.stdout
    assert "--yes" not in help_result.stdout
