from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import marvin_pilot.cli as cli
from marvin_pilot.config import AppConfig
from marvin_pilot.database import DatabaseCredentials, DatabaseMarvinClient


@pytest.mark.parametrize("experimental", [False, True])
def test_client_factory_only_loads_database_credentials_when_explicitly_enabled(
    monkeypatch, experimental
):
    account = SimpleNamespace(account_user_id="123456", account_email="pilot@example.com")
    api = SimpleNamespace(
        api_base_host="https://serv.amazingmarvin.com",
        api_base_url="https://serv.amazingmarvin.com/api",
        check_connection=lambda: account,
        close=lambda: None,
    )
    loaded = []
    databases = []
    account_checks = []
    api.check_connection = lambda: account_checks.append(account) or account
    monkeypatch.setattr(cli, "load_full_access_token", lambda *a, **k: "synthetic-api-token")
    monkeypatch.setattr(cli, "MarvinClient", lambda *a, **k: api)
    credentials = DatabaseCredentials(
        "123456", "pilot@example.com", "https://db.test", "test-db", "user", "synthetic-password"
    )
    monkeypatch.setattr(
        cli, "load_database_credentials", lambda account: loaded.append(account) or credentials
    )
    monkeypatch.setattr(
        cli,
        "CouchDatabase",
        lambda credentials, **kwargs: (
            databases.append(kwargs) or SimpleNamespace(credentials=credentials, close=lambda: None)
        ),
    )
    config = AppConfig(minimum_request_interval_ms=750)
    config._experimental_use_db = experimental
    client = cli._build_client(config, None)
    try:
        assert isinstance(client, DatabaseMarvinClient) is experimental
        assert bool(loaded) is experimental
        if experimental:
            assert api._pacer is databases[0]["pacer"]
            assert api._pacer.minimum_interval_seconds == 3
            assert client.check_preflight_account() == account
            assert account_checks == [account]
            assert client.check_connection() == account
            assert account_checks == [account, account]
    finally:
        client.close()


def test_global_flag_is_explicit_per_invocation_and_not_saved_in_config(monkeypatch):
    config = AppConfig()
    monkeypatch.setattr(cli, "load_config", lambda *_: config)
    seen = []

    def client_factory(config, _):
        seen.append(config._experimental_use_db)
        return SimpleNamespace(
            check_connection=lambda: SimpleNamespace(
                account_email="pilot@example.com",
                account_user_id="123456",
                method="GET",
                request_url="https://api.test/me",
                status_code=200,
                reason_phrase="OK",
            ),
            close=lambda: None,
        )

    monkeypatch.setattr(cli, "_build_client", client_factory)
    runner = CliRunner()
    for args in [["--experimental-use-db", "doctor"], ["doctor"]]:
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
    assert seen == [True, False]
    assert "_experimental_use_db" not in config.model_dump()


def test_forget_db_access_verifies_api_account_without_loading_db_credentials(monkeypatch):
    account = SimpleNamespace(account_user_id="123456", account_email="pilot@example.com")
    forgotten = []
    monkeypatch.setattr(cli, "load_config", lambda *_: AppConfig())

    def client_factory(config, _):
        assert config._experimental_use_db is False
        return SimpleNamespace(check_connection=lambda: account, close=lambda: None)

    monkeypatch.setattr(cli, "_client_from_config", client_factory)
    monkeypatch.setattr(
        cli,
        "forget_database_credentials",
        lambda account: forgotten.append(account.account_user_id),
    )
    result = CliRunner().invoke(
        cli.app, ["--experimental-use-db", "config", "forget-database-access"], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert forgotten == ["123456"]


@pytest.mark.parametrize("matching", [True, False])
def test_database_setup_hides_password_pins_account_and_requires_db_identity(monkeypatch, matching):
    account = SimpleNamespace(account_user_id="123456", account_email="pilot@example.com")
    saved = []
    monkeypatch.setattr(cli, "load_config", lambda *_: AppConfig())
    monkeypatch.setattr(
        cli,
        "_client_from_config",
        lambda *a: SimpleNamespace(check_connection=lambda: account, close=lambda: None),
    )
    monkeypatch.setattr(
        cli,
        "CouchDatabase",
        lambda *a: SimpleNamespace(
            request=lambda _: {"db_name": "test-db" if matching else "wrong-db"}, close=lambda: None
        ),
    )
    monkeypatch.setattr(cli, "store_database_credentials", saved.append)
    result = CliRunner().invoke(
        cli.app,
        ["config", "set-database-access"],
        input="y\nhttps://db.test\ntest-db\nuser\nsynthetic-hidden-password\n",
    )
    assert (result.exit_code == 0) is matching
    assert "synthetic-hidden-password" not in result.output
    assert bool(saved) is matching
    if saved:
        assert (
            saved[0].account_user_id == "123456"
            and saved[0].password == "synthetic-hidden-password"
        )
