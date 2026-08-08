from __future__ import annotations

from pathlib import Path

import pytest

from marvin_pilot.config import (
    AppConfig,
    config_as_toml,
    default_config_path,
    default_history_dir,
    effective_history_dir,
    load_config,
    save_config,
)
from marvin_pilot.errors import PlanSyntaxError


def test_missing_config_uses_safe_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml")
    assert config.credential_mode == "keyring"
    assert config.minimum_request_interval_ms == 750
    assert config.strict_concurrency is True
    assert config.max_operations == 500


def test_config_round_trip_contains_no_token(tmp_path: Path) -> None:
    path = tmp_path / "config" / "config.toml"
    expected = AppConfig(
        credential_mode="file",
        key_file="C:/secure/marvin.key",
        history_dir="C:/audit/marvin",
        minimum_request_interval_ms=1_000,
    )
    save_config(expected, path)
    actual = load_config(path)
    text = path.read_text(encoding="utf-8")
    assert actual == expected
    assert "full_access_token" not in text
    assert "X-Full-Access-Token" not in text


def test_config_renderer_is_stable_toml() -> None:
    rendered = config_as_toml(AppConfig())
    assert rendered.startswith('api_base_url = "https://serv.amazingmarvin.com/api"\n')
    assert 'credential_mode = "keyring"' in rendered
    assert "strict_concurrency = true" in rendered
    assert rendered.endswith("large_plan_warning_operations = 100\n")


@pytest.mark.parametrize(
    "text",
    [
        "this is not = toml = at all",
        "unknown_key = true\n",
        'api_base_url = "https://attacker.invalid/api"\n',
        'credential_mode = "file"\nkey_file = ""\n',
        "max_operations = 10\nlarge_plan_warning_operations = 11\n",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PlanSyntaxError):
        load_config(path)


def test_config_path_must_be_regular_file(tmp_path: Path) -> None:
    with pytest.raises(PlanSyntaxError, match="not a regular file"):
        load_config(tmp_path)


def test_effective_history_path_uses_override() -> None:
    assert effective_history_dir(AppConfig(history_dir="C:/custom/history")) == Path(
        "C:/custom/history"
    )


def test_platform_paths_are_application_specific() -> None:
    assert "marvin-pilot" in str(default_config_path()).lower()
    assert "marvin-pilot" in str(default_history_dir()).lower()
    assert default_config_path().name == "config.toml"
    assert default_history_dir().name == "history"
