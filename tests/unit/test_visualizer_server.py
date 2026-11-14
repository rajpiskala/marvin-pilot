from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import MAX_PLAN_BYTES, parse_plan_bytes
from marvin_pilot.visualizer import build_plan_view
from marvin_pilot.visualizer_hierarchy import build_backup_hierarchy_context
from marvin_pilot.visualizer_server import (
    LOOPBACK_HOST,
    SECURITY_HEADERS,
    LoadedVisualization,
    VisualizerServer,
)


@contextmanager
def running_server(*, preload: bool = True, hierarchy=None, server_kwargs=None):
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode()) if preload else None
    options = dict(server_kwargs or {})
    server = VisualizerServer(
        preloaded_plan=options.pop("preloaded_plan", plan),
        source_name=r"C:\private\plan.json" if preload else None,
        session_token="unit-test-session",
        hierarchy=hierarchy,
        **options,
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
    assert response.json()["plan"] is None
    assert response.json()["can_apply"] is False


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


def test_watched_plan_reloads_and_keeps_last_good_view_on_invalid_edit(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    original = json.dumps(EXAMPLE_PLAN).encode()
    path.write_bytes(original)

    def reload_view() -> LoadedVisualization:
        raw = path.read_bytes()
        return LoadedVisualization(plan=parse_plan_bytes(raw), source_name=path.name, raw=raw)

    with running_server(
        server_kwargs={
            "preloaded_raw": original,
            "watch_paths": (path,),
            "reload_visualization": reload_view,
        }
    ) as (server, client):
        first = client.get(server.url + "api/current").json()
        path.write_bytes(b"not JSON")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            invalid = client.get(server.url + "api/current").json()
            if invalid["revision"] > first["revision"]:
                break
            time.sleep(0.1)
        assert invalid["error"]
        assert invalid["plan"] == first["plan"]
        changed = json.loads(json.dumps(EXAMPLE_PLAN))
        changed["summary"] = "Updated synthetic summary"
        path.write_bytes(json.dumps(changed).encode())
        while time.monotonic() < deadline:
            recovered = client.get(server.url + "api/current").json()
            if recovered["plan"]["summary"] == "Updated synthetic summary":
                break
            time.sleep(0.1)
        assert recovered["error"] is None
        assert recovered["revision"] > invalid["revision"]


def test_reviewed_apply_requires_origin_csrf_fresh_file_and_one_use_challenge(
    tmp_path: Path,
) -> None:
    path = tmp_path / "review.json"
    source = {
        **EXAMPLE_PLAN,
        "expectedAccount": {"userId": "123456", "email": "synthetic@example.com"},
    }
    raw = json.dumps(source).encode()
    path.write_bytes(raw)
    calls = []

    def preflight(plan):
        calls.append("preflight")
        return {
            "operations": len(plan.operations),
            "account": "synthetic@example.com",
            "warnings": [],
            "trash": 0,
        }

    def apply(_plan, _raw, assert_unchanged, reviewed_preflight):
        assert_unchanged()
        assert reviewed_preflight["account"] == "synthetic@example.com"
        calls.append("apply")
        return {"receipt_id": "synthetic-receipt", "receipt_path": "synthetic-receipt.json"}

    with running_server(
        preload=False,
        server_kwargs={
            "preloaded_plan": parse_plan_bytes(raw),
            "preloaded_raw": raw,
            "plan_path": path,
            "review_preflight": preflight,
            "review_apply": apply,
        },
    ) as (server, client):
        current = client.get(server.url + "api/current").json()
        identity = {
            "revision": current["revision"],
            "plan_id": current["plan"]["plan_id"],
            "digest": current["plan"]["digest"],
        }
        payload = {"identity": identity}
        endpoint = server.url + "api/preflight"
        denied = client.post(endpoint, json=payload)
        wrong_origin = client.post(
            endpoint,
            json=payload,
            headers={"Origin": "https://evil.invalid", "X-Marvin-Pilot-CSRF": current["csrf"]},
        )
        headers = {"Origin": server.origin, "X-Marvin-Pilot-CSRF": current["csrf"]}
        checked = client.post(endpoint, json=payload, headers=headers)
        assert checked.status_code == 200
        assert checked.json()["preflight"]["operations"] == len(EXAMPLE_PLAN["operations"])
        applied = client.post(
            server.url + "api/apply",
            json={**payload, "challenge": checked.json()["challenge"]},
            headers=headers,
        )
        replay = client.post(
            server.url + "api/apply",
            json={**payload, "challenge": checked.json()["challenge"]},
            headers=headers,
        )
    assert denied.status_code == 403
    assert wrong_origin.status_code == 403
    assert applied.status_code == 200
    assert applied.json()["result"]["receipt_id"] == "synthetic-receipt"
    assert replay.status_code == 409
    assert calls == ["preflight", "apply"]


def test_reviewed_apply_rejects_changed_file_before_any_mutation(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    source = {
        **EXAMPLE_PLAN,
        "expectedAccount": {"userId": "123456", "email": "synthetic@example.com"},
    }
    raw = json.dumps(source).encode()
    path.write_bytes(raw)
    calls = []
    with running_server(
        preload=False,
        server_kwargs={
            "preloaded_plan": parse_plan_bytes(raw),
            "preloaded_raw": raw,
            "plan_path": path,
            "review_preflight": lambda _plan: {"operations": 6},
            "review_apply": lambda *_args: calls.append("apply"),
        },
    ) as (server, client):
        current = client.get(server.url + "api/current").json()
        identity = {
            "revision": current["revision"],
            "plan_id": current["plan"]["plan_id"],
            "digest": current["plan"]["digest"],
        }
        headers = {"Origin": server.origin, "X-Marvin-Pilot-CSRF": current["csrf"]}
        checked = client.post(
            server.url + "api/preflight", json={"identity": identity}, headers=headers
        )
        assert checked.status_code == 200
        path.write_bytes(raw + b" ")
        stale = client.post(
            server.url + "api/apply",
            json={"identity": identity, "challenge": checked.json()["challenge"]},
            headers=headers,
        )
        assert stale.status_code == 409
        assert "changed" in stale.json()["error"]["message"]
        assert calls == []


def test_text_feedback_is_not_a_plan_and_cannot_be_applied(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    raw = json.dumps(
        {**EXAMPLE_PLAN, "expectedAccount": {"userId": "123456", "email": "pilot@example.com"}}
    ).encode()
    path.write_bytes(raw)
    calls = []
    with running_server(
        preload=False,
        server_kwargs={
            "preloaded_plan": parse_plan_bytes(raw),
            "preloaded_raw": raw,
            "plan_path": path,
            "allow_edit": True,
            "review_preflight": lambda _: calls.append("preflight"),
            "review_apply": lambda *_: calls.append("apply"),
        },
    ) as (server, client):
        current = client.get(server.url + "api/current").json()
        headers = {"Origin": server.origin, "X-Marvin-Pilot-CSRF": current["csrf"]}
        identity = {
            "revision": current["revision"],
            "plan_id": current["plan"]["plan_id"],
            "digest": current["plan"]["digest"],
        }
        started = client.post(
            server.url + "api/edit-start", json={"identity": identity}, headers=headers
        ).json()
        identity = started["identity"]
        payload = {
            "identity": identity,
            "context": {"timeZone": "America/Los_Angeles", "localDate": "2026-11-11"},
            "edits": [
                {
                    "operationId": "create-email-follow-up",
                    "field": "title",
                    "text": "Follow up +4d due Nov 30",
                }
            ],
        }
        rejected = client.post(
            server.url + "api/text-review-save",
            json={**payload, "edits": [{**payload["edits"][0], "action": "trash"}]},
            headers=headers,
        )
        assert rejected.status_code == 409
        assert list(tmp_path.iterdir()) == [path]
        saved = client.post(server.url + "api/text-review-save", json=payload, headers=headers)
        assert saved.status_code == 200, saved.text
        result = saved.json()
        review_path = Path(result["saved_path"])
        assert review_path.parent == path.parent and review_path != path
        assert json.loads(review_path.read_text())["requiresAIReconciliation"] is True
        assert result["can_apply"] is False
        assert path.read_bytes() == raw
        denied = client.post(
            server.url + "api/preflight", json={"identity": identity}, headers=headers
        )
        assert denied.status_code == 409
        cancelled = client.post(
            server.url + "api/edit-cancel", json={"identity": identity}, headers=headers
        ).json()
        assert cancelled["can_apply"] is False  # Saved requests still need AI reconciliation.
        assert cancelled["text_review_path"] == str(review_path)
        assert calls == []


def test_autosave_shared_across_servers_restart_and_blocks_apply(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    raw = json.dumps(
        {**EXAMPLE_PLAN, "expectedAccount": {"userId": "123456", "email": "pilot@example.com"}}
    ).encode()
    path.write_bytes(raw)
    calls = []
    options = {
        "preloaded_plan": parse_plan_bytes(raw),
        "preloaded_raw": raw,
        "plan_path": path,
        "allow_edit": True,
        "review_preflight": lambda _: {"operations": 6},
        "review_apply": lambda *_: calls.append("apply"),
    }
    with (
        running_server(server_kwargs=options) as (a, client),
        running_server(server_kwargs=options) as (b, other),
    ):
        current = client.get(a.url + "api/current").json()
        identity = {
            "revision": current["revision"],
            "plan_id": current["plan"]["plan_id"],
            "digest": current["plan"]["digest"],
        }
        headers = {"Origin": a.origin, "X-Marvin-Pilot-CSRF": current["csrf"]}
        checked = client.post(a.url + "api/preflight", json={"identity": identity}, headers=headers)
        assert checked.status_code == 200
        payload = {
            "identity": identity,
            "clientId": str(uuid4()),
            "sequence": 1,
            "context": {"timeZone": "UTC", "localDate": "2026-11-13"},
            "edits": [
                {"operationId": "create-email-follow-up", "field": "title", "text": "Edited by B"}
            ],
        }
        denied = client.post(a.url + "api/text-review-patch", json=payload)
        assert denied.status_code == 403
        saved = other.post(
            b.url + "api/text-review-patch",
            json=payload,
            headers={"Origin": b.origin, "X-Marvin-Pilot-CSRF": b.csrf_token},
        )
        assert saved.status_code == 200, saved.text
        assert (
            saved.json()["revision"] == current["revision"]
        )  # Another tab's identity stays valid.
        assert not saved.json()["editing"]
        status = client.get(a.url + "api/status").json()
        assert status["text_version"] == 1 and status["text_pending"]
        assert status["text_edits"] == []  # Polls do not retransmit all feedback.
        assert not status["can_apply"]
        state = client.get(a.url + "api/text-state").json()
        assert state["edits"][0]["text"] == "Edited by B"
        assert Path(state["path"]).parent == path.parent
        blocked = client.post(
            a.url + "api/apply",
            json={"identity": identity, "challenge": checked.json()["challenge"]},
            headers=headers,
        )
        assert blocked.status_code == 409 and calls == []
        replay = other.post(
            b.url + "api/text-review-patch",
            json=payload,
            headers={"Origin": b.origin, "X-Marvin-Pilot-CSRF": b.csrf_token},
        )
        assert replay.json()["text_version"] == 1
        # An edit-and-restore in another process still invalidates old approval.
        original_title = EXAMPLE_PLAN["operations"][2]["after"]["title"]
        reset = other.post(
            b.url + "api/text-review-patch",
            json={
                **payload,
                "sequence": 2,
                "edits": [{**payload["edits"][0], "text": original_title}],
            },
            headers={"Origin": b.origin, "X-Marvin-Pilot-CSRF": b.csrf_token},
        )
        assert reset.json()["can_apply"]
        checked = client.post(a.url + "api/preflight", json={"identity": identity}, headers=headers)
        assert checked.status_code == 200
        for sequence, text in ((3, "Temporary edit"), (4, original_title)):
            changed = other.post(
                b.url + "api/text-review-patch",
                json={
                    **payload,
                    "sequence": sequence,
                    "edits": [{**payload["edits"][0], "text": text}],
                },
                headers={"Origin": b.origin, "X-Marvin-Pilot-CSRF": b.csrf_token},
            )
            assert changed.status_code == 200
        expired = client.post(
            a.url + "api/apply",
            json={"identity": identity, "challenge": checked.json()["challenge"]},
            headers=headers,
        )
        assert expired.status_code == 409 and "preflight" in expired.json()["error"]["message"]
        other.post(
            b.url + "api/text-review-patch",
            json={**payload, "sequence": 5},
            headers={"Origin": b.origin, "X-Marvin-Pilot-CSRF": b.csrf_token},
        ).raise_for_status()
        path.write_bytes(raw + b" ")
        stale = client.post(
            a.url + "api/text-review-patch", json={**payload, "sequence": 2}, headers=headers
        )
        assert stale.status_code == 409 and "changed" in stale.json()["error"]["message"]
        path.write_bytes(raw)
    with running_server(server_kwargs=options) as (fresh, client):
        restored = client.get(fresh.url + "api/current").json()
        assert restored["text_edits"][0]["text"] == "Edited by B"
        assert not restored["can_apply"] and restored["can_edit"]
        assert path.read_bytes() == raw


@pytest.mark.parametrize(
    "damage", ["empty", "context", "structure", "writer", "sequence", "identity", "corrupt"]
)
def test_autosave_rejects_invalid_or_corrupt_feedback(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "review.json"
    raw = json.dumps(EXAMPLE_PLAN).encode()
    path.write_bytes(raw)
    with running_server(
        server_kwargs={"preloaded_raw": raw, "plan_path": path, "allow_edit": True}
    ) as (server, client):
        current = client.get(server.url + "api/current").json()
        payload = {
            "identity": {
                "revision": current["revision"],
                "plan_id": current["plan"]["plan_id"],
                "digest": current["plan"]["digest"],
            },
            "clientId": str(uuid4()),
            "sequence": 1,
            "context": {"timeZone": "UTC", "localDate": "2026-11-13"},
            "edits": [
                {"operationId": "create-email-follow-up", "field": "title", "text": "Changed"}
            ],
        }
        if damage == "empty":
            payload["edits"] = None
        if damage == "context":
            payload["context"] = None
        if damage == "structure":
            payload["edits"][0]["action"] = "trash"
        if damage == "writer":
            payload["clientId"] = "bad"
        if damage == "sequence":
            payload["sequence"] = False
        if damage == "identity":
            payload["identity"]["revision"] += 1
        if damage == "corrupt":
            server._text_store().path.write_bytes(b"broken")
        rejected = client.post(
            server.url + "api/text-review-patch",
            json=payload,
            headers={"Origin": server.origin, "X-Marvin-Pilot-CSRF": server.csrf_token},
        )
        assert rejected.status_code == 409, rejected.text
        assert path.read_bytes() == raw
        if damage == "corrupt":
            for route in ("api/current", "api/status", "api/text-state"):
                assert client.get(server.url + route).status_code == 409


def test_revision_preview_save_preserves_source_and_invalidates_apply(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    raw = json.dumps(
        {**EXAMPLE_PLAN, "expectedAccount": {"userId": "123456", "email": "pilot@example.com"}}
    ).encode()
    path.write_bytes(raw)
    calls = []
    with running_server(
        preload=False,
        server_kwargs={
            "preloaded_plan": parse_plan_bytes(raw),
            "preloaded_raw": raw,
            "plan_path": path,
            "allow_edit": True,
            "watch_paths": (path,),
            "review_preflight": lambda _: {"operations": 4},
            "review_apply": lambda *_: calls.append("apply"),
        },
    ) as (server, client):
        current = client.get(server.url + "api/current").json()
        identity = {
            "revision": current["revision"],
            "plan_id": current["plan"]["plan_id"],
            "digest": current["plan"]["digest"],
        }
        headers = {"Origin": server.origin, "X-Marvin-Pilot-CSRF": current["csrf"]}

        def post(route, **payload):
            return client.post(
                server.url + "api/" + route, json={"identity": identity, **payload}, headers=headers
            )

        checked = post("preflight").json()
        assert post("edit-start").status_code == 200
        started = client.get(server.url + "api/status").json()
        assert started["editing"] is True
        assert started["can_apply"] is False
        stale = post("apply", challenge=checked["challenge"])
        assert stale.status_code == 409
        identity["revision"] = started["revision"]
        edits = [{"operationId": "create-email-follow-up", "title": "A better proposed title"}]
        preview = post("revision-preview", edits=edits)
        assert preview.status_code == 200, preview.text
        candidate = preview.json()
        assert candidate["plan"]["plan_id"] != current["plan"]["plan_id"]
        assert list(tmp_path.iterdir()) == [path]
        denied = post("revision-save", revision_token="wrong")
        assert denied.status_code == 409
        saved = post("revision-save", revision_token=candidate["revision_token"])
        assert saved.status_code == 200, saved.text
        result = saved.json()
        destination = Path(result["saved_path"])
        assert destination.parent == path.parent
        assert destination != path
        assert path.read_bytes() == raw
        assert parse_plan_bytes(destination.read_bytes()).planId == candidate["plan"]["plan_id"]
        assert result["can_apply"] is True
        assert result["session_key"] is None
        assert destination.with_suffix(".review.json").is_file()
        assert post("revision-save", revision_token=candidate["revision_token"]).status_code == 409
        assert calls == []


@pytest.mark.parametrize("mode", ["csrf", "disabled", "applied", "stale", "unknown-edit"])
def test_editor_rejects_unsafe_requests_without_writes(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "review.json"
    raw = json.dumps(EXAMPLE_PLAN).encode()
    path.write_bytes(raw)
    with running_server(
        preload=False,
        server_kwargs={
            "preloaded_plan": parse_plan_bytes(raw),
            "preloaded_raw": raw,
            "plan_path": path,
            "allow_edit": mode != "disabled",
            "review_state": "applied" if mode == "applied" else "preview",
        },
    ) as (server, client):
        current = client.get(server.url + "api/current").json()
        identity = {
            "revision": current["revision"],
            "plan_id": current["plan"]["plan_id"],
            "digest": current["plan"]["digest"],
        }
        headers = {
            "Origin": server.origin,
            "X-Marvin-Pilot-CSRF": "wrong" if mode == "csrf" else current["csrf"],
        }
        if mode == "stale":
            path.write_bytes(raw + b" ")
        response = client.post(
            server.url + "api/edit-start", json={"identity": identity}, headers=headers
        )
        if mode == "unknown-edit":
            assert response.status_code == 200
            identity = response.json()["identity"]
            response = client.post(
                server.url + "api/revision-preview",
                json={"identity": identity, "edits": [{"operationId": "unknown", "exclude": True}]},
                headers=headers,
            )
        assert response.status_code in {403, 409}
        assert list(tmp_path.iterdir()) == [path]


def test_cancel_edit_and_changed_source_cannot_save_stale_candidate(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    raw = json.dumps(EXAMPLE_PLAN).encode()
    path.write_bytes(raw)
    with running_server(
        preload=False,
        server_kwargs={
            "preloaded_plan": parse_plan_bytes(raw),
            "preloaded_raw": raw,
            "plan_path": path,
            "allow_edit": True,
        },
    ) as (server, client):
        current = client.get(server.url + "api/current").json()
        identity = {
            "revision": current["revision"],
            "plan_id": current["plan"]["plan_id"],
            "digest": current["plan"]["digest"],
        }
        headers = {"Origin": server.origin, "X-Marvin-Pilot-CSRF": current["csrf"]}
        start = client.post(
            server.url + "api/edit-start", json={"identity": identity}, headers=headers
        ).json()
        identity = start["identity"]
        preview = client.post(
            server.url + "api/revision-preview",
            json={
                "identity": identity,
                "edits": [{"operationId": "create-email-follow-up", "title": "Revised title"}],
            },
            headers=headers,
        ).json()
        path.write_bytes(raw + b" ")
        saved = client.post(
            server.url + "api/revision-save",
            json={"identity": identity, "revision_token": preview["revision_token"]},
            headers=headers,
        )
        assert saved.status_code == 409
        cancelled = client.post(
            server.url + "api/edit-cancel", json={"identity": identity}, headers=headers
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["editing"] is False
        assert cancelled.json()["plan"]["plan_id"] == current["plan"]["plan_id"]
        assert list(tmp_path.iterdir()) == [path]
