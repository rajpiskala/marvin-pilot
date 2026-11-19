from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

import marvin_pilot.cli as cli
from marvin_pilot.marvin_client import MarvinClient


@pytest.mark.parametrize("write_output", [False, True])
def test_live_search_checks_account_then_reads_one_collection(monkeypatch, tmp_path, write_output):
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.headers["X-Full-Access-Token"] == "synthetic-secret"
        if request.url.path == "/api/me":
            return httpx.Response(200, json={"userId": "123456", "email": "pilot@example.com"})
        assert request.url.path == "/api/categories"
        return httpx.Response(
            200,
            json=[
                {"_id": "area", "title": "Example Area", "type": "category", "parentId": "root"},
                {
                    "_id": "project",
                    "title": "Example Project",
                    "type": "project",
                    "parentId": "area",
                    "done": True,
                },
            ],
        )

    client = MarvinClient(
        "synthetic-secret",
        api_base_url="https://marvin.test/api",
        transport=httpx.MockTransport(handle),
        minimum_request_interval_ms=0,
    )
    monkeypatch.setattr(cli, "_client_from_config", lambda *_: client)
    monkeypatch.setattr(cli, "_load_config_or_fail", lambda: None)
    output = tmp_path / "result.json"
    args = ["context", "search", "example", "--live"]
    if write_output:
        args += ["--output", str(output)]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text() if write_output else result.stdout)
    assert payload["kind"] == "search-live"
    assert payload["account"]["userId"] == "123456"
    assert payload["ambiguous"] is True
    assert [p["title"] for p in payload["candidates"][1]["path"]] == [
        "Example Area",
        "Example Project",
    ]
    assert payload["candidates"][1]["done"] is True
    assert calls == [("GET", "/api/me"), ("GET", "/api/categories")]


@pytest.mark.parametrize(
    "args",
    [
        ["context", "search", "example"],
        ["context", "search", "example", "--live", "--backup", "unused.json"],
        [
            "context",
            "search",
            "example",
            "--backup",
            "unused.json",
            "--full-access-key-file",
            "unused.key",
        ],
    ],
)
def test_search_mode_errors_never_load_credentials(monkeypatch, args):
    monkeypatch.setattr(cli, "_client_from_config", lambda *_: pytest.fail("must stay offline"))
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2


def test_live_search_auth_failure_stops_before_collection(monkeypatch):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={"error": "unauthorized"})

    client = MarvinClient(
        "synthetic-secret",
        api_base_url="https://marvin.test/api",
        transport=httpx.MockTransport(handle),
        minimum_request_interval_ms=0,
    )
    monkeypatch.setattr(cli, "_client_from_config", lambda *_: client)
    monkeypatch.setattr(cli, "_load_config_or_fail", lambda: None)
    result = CliRunner().invoke(cli.app, ["context", "search", "example", "--live"])
    assert result.exit_code == 4
    assert calls == ["/api/me"]
