from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.client import HTTPConnection

import httpx

from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import MAX_PLAN_BYTES, parse_plan_bytes
from marvin_pilot.visualizer import build_plan_view
from marvin_pilot.visualizer_hierarchy import build_backup_hierarchy_context
from marvin_pilot.visualizer_server import LOOPBACK_HOST, SECURITY_HEADERS, VisualizerServer


@contextmanager
def running_server(*, preload: bool = True, hierarchy=None):
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode()) if preload else None
    server = VisualizerServer(
        preloaded_plan=plan,
        source_name=r"C:\private\plan.json" if preload else None,
        session_token="unit-test-session",
        hierarchy=hierarchy,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            yield server, client
    finally:
        server.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_root_assets_and_security_headers_are_served_without_plan_content() -> None:
    with running_server() as (server, client):
        response = client.get(server.url)
        script = client.get(server.url + "app.js")
        stylesheet = client.get(server.url + "styles.css")
        artwork = client.get(server.url + "marvin-pilot.png")
        favicon = client.get(server.url + "favicon-64.png")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Marvin Pilot - Plan preview" in response.text
    assert "Wash the dishes" not in response.text
    assert script.status_code == 200
    assert stylesheet.status_code == 200
    assert artwork.status_code == 200
    assert artwork.headers["content-type"] == "image/png"
    assert artwork.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/png"
    assert len(favicon.content) < 16_000
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_preloaded_and_uploaded_plans_return_the_same_view() -> None:
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    expected = build_plan_view(plan, source_name="plan.json").to_dict()
    with running_server() as (server, client):
        current = client.get(server.url + "api/current")
        uploaded = client.post(
            server.url + "api/plan",
            content=json.dumps(EXAMPLE_PLAN).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": server.origin,
                "X-Marvin-Pilot-Filename": r"C:\private\plan.json",
            },
        )
    assert current.status_code == 200
    assert uploaded.status_code == 200
    assert current.json()["plan"] == expected
    assert uploaded.json()["plan"] == expected


def test_empty_server_reports_no_current_plan() -> None:
    with running_server(preload=False) as (server, client):
        response = client.get(server.url + "api/current")
    assert response.status_code == 200
    assert response.json() == {"plan": None}


def test_backup_hierarchy_is_reused_for_browser_uploaded_plans() -> None:
    upload_plan = json.loads(json.dumps(EXAMPLE_PLAN))
    upload_plan["operations"][0].pop("display")
    hierarchy = build_backup_hierarchy_context(
        [
            {
                "_id": "home-category",
                "db": "Categories",
                "type": "category",
                "title": "Household",
                "parentId": "root",
            },
            {
                "_id": "task-wash-dishes-id",
                "db": "Tasks",
                "title": "Wash the dishes",
                "parentId": "home-category",
            },
        ]
    )
    with running_server(preload=False, hierarchy=hierarchy) as (server, client):
        uploaded = client.post(
            server.url + "api/plan",
            content=json.dumps(upload_plan).encode(),
            headers={"Content-Type": "application/json", "Origin": server.origin},
        )

    assert uploaded.status_code == 200
    plan = uploaded.json()["plan"]
    assert plan["hierarchy_source"] == "backup"
    first = plan["operations"][0]
    assert first["before_path_state"] == "path"
    assert first["before_path"][-1]["title"] == "Household"


def test_session_host_origin_route_and_methods_are_restricted() -> None:
    with running_server() as (server, client):
        wrong_session = client.get(f"{server.origin}/wrong/")
        wrong_host = client.get(server.url, headers={"Host": "attacker.invalid"})
        wrong_origin = client.get(server.url, headers={"Origin": "https://attacker.invalid"})
        traversal = client.get(server.url + "../api/current")
        put = client.put(server.url + "api/current", content=b"{}")
    assert wrong_session.status_code == 404
    assert wrong_host.status_code == 421
    assert wrong_origin.status_code == 403
    assert traversal.status_code == 404
    assert put.status_code == 405


def test_upload_content_type_size_and_validation_fail_safely() -> None:
    with running_server(preload=False) as (server, client):
        wrong_type = client.post(server.url + "api/plan", content=b"{}")
        connection = HTTPConnection(LOOPBACK_HOST, server.port, timeout=5)
        connection.putrequest("POST", f"/{server.session_token}/api/plan")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_PLAN_BYTES + 1))
        connection.endheaders()
        too_large_status = connection.getresponse().status
        connection.close()
        invalid = client.post(
            server.url + "api/plan",
            content=b'{"summary":"private title that must not be repeated wholesale"}',
            headers={"Content-Type": "application/json"},
        )
    assert wrong_type.status_code == 415
    assert too_large_status == 413
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == 2
    assert "private title that must not be repeated wholesale" not in invalid.text
    assert invalid.headers["cache-control"] == "no-store"


def test_server_writes_no_access_log(capsys) -> None:
    with running_server() as (server, client):
        assert client.get(server.url).status_code == 200
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_shutdown_releases_the_bound_port() -> None:
    server = VisualizerServer(session_token="port-release")
    port = server.port
    server.shutdown()
    with socket.socket() as probe:
        probe.bind((LOOPBACK_HOST, port))
