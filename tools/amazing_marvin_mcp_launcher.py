"""Launch Amazing Marvin MCP with its limited key from the native OS keyring."""

from __future__ import annotations

import getpass
import os
import runpy
import sys

import keyring
from keyring.errors import KeyringError

KEYRING_SERVICE = "amazing-marvin-mcp"
KEYRING_ACCOUNT = "limited-api-key"
TOKEN_ENVIRONMENT_VARIABLE = "AMAZING_MARVIN_API_KEY"


def _validated_token(value: str) -> str:
    token = value.strip()
    if not token:
        raise ValueError("the Amazing Marvin limited API key is empty")
    if any(character.isspace() for character in token):
        raise ValueError("the Amazing Marvin limited API key contains whitespace")
    if len(token) > 4096:
        raise ValueError("the Amazing Marvin limited API key is unexpectedly large")
    return token


def _store_token() -> int:
    try:
        token = _validated_token(getpass.getpass("Amazing Marvin limited API key: "))
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, token)
    except (EOFError, KeyboardInterrupt):
        print("Credential setup cancelled.", file=sys.stderr)
        return 1
    except (KeyringError, ValueError) as exc:
        print(f"Could not store the Amazing Marvin API key: {exc}", file=sys.stderr)
        return 1
    print("Stored the limited API key in the native OS keyring.")
    return 0


def _load_token() -> str:
    try:
        value = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    except KeyringError as exc:
        raise RuntimeError(f"the native OS keyring could not be read: {exc}") from exc
    if value is None:
        raise RuntimeError(
            "no Amazing Marvin limited API key is stored; run this launcher with --store-key"
        )
    return _validated_token(value)


def main() -> int:
    if sys.argv[1:] == ["--store-key"]:
        return _store_token()
    if sys.argv[1:]:
        print("Usage: amazing_marvin_mcp_launcher.py [--store-key]", file=sys.stderr)
        return 2
    try:
        os.environ[TOKEN_ENVIRONMENT_VARIABLE] = _load_token()
    except (RuntimeError, ValueError) as exc:
        print(f"Amazing Marvin MCP cannot start: {exc}", file=sys.stderr)
        return 1
    runpy.run_module("amazing_marvin_mcp", run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
