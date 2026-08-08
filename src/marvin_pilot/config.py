"""Cross-platform, non-secret Marvin Pilot configuration."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Literal

from platformdirs import PlatformDirs
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from marvin_pilot.atomic import atomic_write_bytes
from marvin_pilot.errors import PlanSyntaxError

DEFAULT_API_BASE_URL = "https://serv.amazingmarvin.com/api"
APP_NAME = "marvin-pilot"


class AppConfig(BaseModel):
    """Non-secret settings. The full-access token must never be stored here."""

    model_config = ConfigDict(extra="forbid")

    api_base_url: StrictStr = DEFAULT_API_BASE_URL
    credential_mode: Literal["keyring", "prompt", "file"] = "keyring"
    history_dir: StrictStr = ""
    key_file: StrictStr = ""
    strict_concurrency: StrictBool = True
    request_timeout_seconds: StrictFloat | StrictInt = Field(default=20.0, gt=0, le=300)
    minimum_request_interval_ms: StrictInt = Field(default=750, ge=0, le=60_000)
    max_operations: StrictInt = Field(default=500, ge=1, le=500)
    large_plan_warning_operations: StrictInt = Field(default=100, ge=1, le=500)

    @field_validator("api_base_url")
    @classmethod
    def validate_api_base_url(cls, value: str) -> str:
        if value != DEFAULT_API_BASE_URL:
            raise ValueError(
                f"api_base_url must remain fixed to {DEFAULT_API_BASE_URL!r} "
                "in normal configuration"
            )
        return value

    @field_validator("history_dir", "key_file")
    @classmethod
    def reject_nul_in_paths(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("paths must not contain NUL bytes")
        return value

    @model_validator(mode="after")
    def validate_warning_threshold(self) -> AppConfig:
        if self.large_plan_warning_operations > self.max_operations:
            raise ValueError("large_plan_warning_operations must not exceed max_operations")
        if self.credential_mode == "file" and not self.key_file:
            raise ValueError("key_file is required when credential_mode is 'file'")
        return self


def default_config_path() -> Path:
    """Return the OS-native roaming configuration file path."""

    return (
        Path(PlatformDirs(APP_NAME, appauthor=False, roaming=True).user_config_dir) / "config.toml"
    )


def default_history_dir() -> Path:
    """Return the OS-native local application-data receipt directory."""

    return Path(PlatformDirs(APP_NAME, appauthor=False, roaming=False).user_data_dir) / "history"


def effective_history_dir(config: AppConfig) -> Path:
    """Resolve a configured history path or the platform default."""

    return Path(config.history_dir).expanduser() if config.history_dir else default_history_dir()


def load_config(path: Path | None = None) -> AppConfig:
    """Load strict TOML, returning safe defaults when no file exists."""

    config_path = path or default_config_path()
    if not config_path.exists():
        return AppConfig()
    try:
        if not config_path.is_file():
            raise PlanSyntaxError(f"config path is not a regular file: {config_path}")
        with config_path.open("rb") as file:
            raw = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise PlanSyntaxError(f"invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise PlanSyntaxError(f"could not read config {config_path}: {exc}") from exc
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        messages = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            messages.append(f"{location}: {error['msg']}")
        raise PlanSyntaxError("config validation failed:\n- " + "\n- ".join(messages)) from exc


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def config_as_toml(config: AppConfig) -> str:
    """Render stable TOML containing only non-secret settings."""

    values = config.model_dump()
    lines = [
        f"api_base_url = {_toml_string(values['api_base_url'])}",
        f"credential_mode = {_toml_string(values['credential_mode'])}",
        f"history_dir = {_toml_string(values['history_dir'])}",
        f"key_file = {_toml_string(values['key_file'])}",
        f"strict_concurrency = {str(values['strict_concurrency']).lower()}",
        f"request_timeout_seconds = {float(values['request_timeout_seconds'])}",
        f"minimum_request_interval_ms = {values['minimum_request_interval_ms']}",
        f"max_operations = {values['max_operations']}",
        f"large_plan_warning_operations = {values['large_plan_warning_operations']}",
    ]
    return "\n".join(lines) + "\n"


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    """Atomically persist non-secret configuration."""

    config_path = path or default_config_path()
    atomic_write_bytes(config_path, config_as_toml(config).encode("utf-8"))
    return config_path
