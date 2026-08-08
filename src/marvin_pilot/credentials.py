"""Full-access credential loading without arguments or environment variables."""

from __future__ import annotations

import getpass
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from marvin_pilot.config import AppConfig
from marvin_pilot.errors import CredentialError

KEYRING_SERVICE = "marvin-pilot"
KEYRING_ACCOUNT = "amazing-marvin-full-access-token"
MAX_TOKEN_FILE_BYTES = 8 * 1024


class KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


def _validate_token(token: str) -> str:
    value = token.strip()
    if not value:
        raise CredentialError("the full-access token is empty")
    if any(character.isspace() for character in value):
        raise CredentialError("the full-access token must not contain whitespace")
    if len(value) > MAX_TOKEN_FILE_BYTES:
        raise CredentialError("the full-access token is unexpectedly large")
    return value


def load_token_file(path: Path, warn: Callable[[str], None] | None = None) -> str:
    """Read a small regular file and warn about broad Unix permissions."""

    warning = warn or (lambda _message: None)
    try:
        if not path.is_file():
            raise CredentialError(f"full-access key path is not a regular file: {path}")
        if path.stat().st_size > MAX_TOKEN_FILE_BYTES:
            raise CredentialError("the full-access key file is unexpectedly large")
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            warning(f"full-access key file is readable by other users: {path}")
        token = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialError("the full-access key file must be UTF-8 text") from exc
    except OSError as exc:
        raise CredentialError(f"could not read full-access key file {path}: {exc}") from exc
    return _validate_token(token)


def load_full_access_token(
    config: AppConfig,
    *,
    key_file_override: Path | None = None,
    stdin: TextIO | None = None,
    prompt: Callable[[str], str] | None = None,
    keyring_backend: KeyringBackend = keyring,
    warn: Callable[[str], None] | None = None,
) -> str:
    """Load a token using explicit file override, keyring, prompt, or configured file."""

    if key_file_override is not None:
        return load_token_file(key_file_override, warn)

    if config.credential_mode == "file":
        return load_token_file(Path(config.key_file).expanduser(), warn)

    if config.credential_mode == "prompt":
        input_stream = stdin or sys.stdin
        if not input_stream.isatty():
            raise CredentialError(
                "prompt credential mode requires an interactive terminal; "
                "redirected stdin is refused"
            )
        prompt_function = prompt or getpass.getpass
        return _validate_token(prompt_function("Amazing Marvin full-access token: "))

    try:
        token = keyring_backend.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except KeyringError as exc:
        raise CredentialError(f"the operating-system keyring could not be read: {exc}") from exc
    if token is None:
        raise CredentialError(
            "no full-access token is stored; run 'marvin-pilot config set-full-access-token'"
        )
    return _validate_token(token)


def store_keyring_token(token: str, keyring_backend: KeyringBackend = keyring) -> None:
    """Store a validated token in the native keyring."""

    try:
        keyring_backend.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, _validate_token(token))
    except KeyringError as exc:
        raise CredentialError(
            f"the operating-system keyring could not store the token: {exc}"
        ) from exc


def delete_keyring_token(keyring_backend: KeyringBackend = keyring) -> None:
    """Remove Marvin Pilot's token from the native keyring."""

    try:
        keyring_backend.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except PasswordDeleteError:
        return
    except KeyringError as exc:
        raise CredentialError(
            f"the operating-system keyring could not remove the token: {exc}"
        ) from exc
