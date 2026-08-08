from __future__ import annotations

from pathlib import Path

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

from marvin_pilot.config import AppConfig
from marvin_pilot.credentials import (
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    delete_keyring_token,
    load_full_access_token,
    load_token_file,
    store_keyring_token,
)
from marvin_pilot.errors import CredentialError


class FakeKeyring:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.calls: list[tuple] = []

    def get_password(self, service_name: str, username: str) -> str | None:
        self.calls.append(("get", service_name, username))
        return self.token

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.calls.append(("set", service_name, username, password))
        self.token = password

    def delete_password(self, service_name: str, username: str) -> None:
        self.calls.append(("delete", service_name, username))
        self.token = None


class ErrorKeyring(FakeKeyring):
    def get_password(self, service_name: str, username: str) -> str | None:
        raise KeyringError("locked")

    def set_password(self, service_name: str, username: str, password: str) -> None:
        raise KeyringError("locked")

    def delete_password(self, service_name: str, username: str) -> None:
        raise KeyringError("locked")


class MissingDeleteKeyring(FakeKeyring):
    def delete_password(self, service_name: str, username: str) -> None:
        raise PasswordDeleteError("missing")


class FakeInput:
    def __init__(self, is_tty: bool) -> None:
        self.is_tty = is_tty

    def isatty(self) -> bool:
        return self.is_tty


def test_keyring_mode_reads_only_named_entry() -> None:
    backend = FakeKeyring("stored-token")
    assert load_full_access_token(AppConfig(), keyring_backend=backend) == "stored-token"
    assert backend.calls == [("get", KEYRING_SERVICE, KEYRING_ACCOUNT)]


def test_missing_and_failing_keyring_are_actionable() -> None:
    with pytest.raises(CredentialError, match="no full-access token"):
        load_full_access_token(AppConfig(), keyring_backend=FakeKeyring())
    with pytest.raises(CredentialError, match="could not be read"):
        load_full_access_token(AppConfig(), keyring_backend=ErrorKeyring())


def test_prompt_mode_requires_tty_and_hidden_prompt() -> None:
    config = AppConfig(credential_mode="prompt")
    prompts: list[str] = []

    def prompt(message: str) -> str:
        prompts.append(message)
        return "prompted-token"

    assert (
        load_full_access_token(config, stdin=FakeInput(True), prompt=prompt)  # type: ignore[arg-type]
        == "prompted-token"
    )
    assert prompts == ["Amazing Marvin full-access token: "]
    with pytest.raises(CredentialError, match="redirected stdin is refused"):
        load_full_access_token(config, stdin=FakeInput(False))  # type: ignore[arg-type]


def test_file_mode_and_override_precedence(tmp_path: Path) -> None:
    configured = tmp_path / "configured.key"
    override = tmp_path / "override.key"
    configured.write_text("configured-token\n", encoding="utf-8")
    override.write_text("override-token\n", encoding="utf-8")
    config = AppConfig(credential_mode="file", key_file=str(configured))
    assert load_full_access_token(config) == "configured-token"
    assert load_full_access_token(config, key_file_override=override) == "override-token"


@pytest.mark.parametrize("content", ["", "two words", "line-one\nline-two"])
def test_invalid_file_tokens_are_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "token.key"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(CredentialError):
        load_token_file(path)


def test_token_path_must_be_small_regular_file(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="not a regular file"):
        load_token_file(tmp_path)
    path = tmp_path / "large.key"
    path.write_text("x" * 9_000, encoding="utf-8")
    with pytest.raises(CredentialError, match="unexpectedly large"):
        load_token_file(path)


def test_store_and_delete_use_native_keyring_entry() -> None:
    backend = FakeKeyring()
    store_keyring_token("secret-token", backend)
    assert backend.token == "secret-token"
    delete_keyring_token(backend)
    assert backend.token is None


def test_keyring_write_errors_are_redacted() -> None:
    with pytest.raises(CredentialError, match="could not store") as store_error:
        store_keyring_token("do-not-leak", ErrorKeyring())
    assert "do-not-leak" not in str(store_error.value)
    with pytest.raises(CredentialError, match="could not remove"):
        delete_keyring_token(ErrorKeyring())
    delete_keyring_token(MissingDeleteKeyring())
