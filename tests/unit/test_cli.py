from __future__ import annotations

import copy
import io
import json
from datetime import datetime
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

import marvin_pilot.cli as cli_module
import marvin_pilot.config as config_module
from marvin_pilot import __version__
from marvin_pilot.cli import app
from marvin_pilot.errors import CredentialError, RemoteError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.field_registry import FIELD_SPECS
from marvin_pilot.history import receipt_file_bytes
from marvin_pilot.marvin_client import ConnectionCheck
from marvin_pilot.models.receipt_v1 import ReceiptOperationV1, ReceiptV1
from marvin_pilot.plan_io import parse_plan_bytes, plan_digest

runner = CliRunner()


class CliMarvinClient:
    api_base_host = "https://marvin.test"
    api_base_url = "https://marvin.test/api"

    def __init__(self, document: dict) -> None:
        self.document = copy.deepcopy(document)
        self.closed = False
        self.mutations = 0
        self.connection_checks = 0

    def check_connection(self) -> ConnectionCheck:
        self.connection_checks += 1
        return ConnectionCheck(
            account_email="pilot@example.com",
            account_user_id="123456",
            method="GET",
            request_url="https://marvin.test/api/me",
            status_code=200,
            reason_phrase="OK",
        )

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

    def delete_doc(self, item_id: str):
        assert item_id == self.document["_id"]
        self.document = {}
        self.mutations += 1
        return {"ok": True}

    def close(self) -> None:
        self.closed = True


class ReadOnlyCliMarvinClient:
    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = copy.deepcopy(documents)
        self.reads: list[str] = []
        self.label_reads = 0
        self.closed = False

    def get_doc(self, item_id: str):
        self.reads.append(item_id)
        document = self.documents.get(item_id)
        return copy.deepcopy(document) if document is not None else None

    def get_labels(self):
        self.label_reads += 1
        return [
            copy.deepcopy(document | {"_id": item_id})
            for item_id, document in self.documents.items()
            if document.get("db") == "Labels"
        ]

    def close(self) -> None:
        self.closed = True


class MultiCliMarvinClient:
    api_base_host = "https://marvin.test"

    def __init__(self, documents: dict[str, dict]) -> None:
        self.documents = copy.deepcopy(documents)
        self.revision = 1
        self.closed = False

    def check_connection(self) -> ConnectionCheck:
        return ConnectionCheck(
            account_email="pilot@example.com",
            account_user_id="123456",
            method="GET",
            request_url="https://marvin.test/api/me",
            status_code=200,
            reason_phrase="OK",
        )

    def get_doc(self, item_id: str):
        document = self.documents.get(item_id)
        return copy.deepcopy(document) if document is not None else None

    def get_labels(self):
        return []

    def get_children(self, parent_id: str):
        return [
            copy.deepcopy(document)
            for document in self.documents.values()
            if document.get("parentId") == parent_id
        ]

    def update_doc(self, item_id: str, setters: list[dict]):
        document = self.documents[item_id]
        for setter in setters:
            key = setter["key"]
            if key.startswith("fieldUpdates."):
                document.setdefault("fieldUpdates", {})[key.split(".", 1)[1]] = setter["val"]
            else:
                document[key] = setter["val"]
        self.revision += 1
        document["_rev"] = f"{self.revision}-updated"
        return copy.deepcopy(document)

    def create_doc(self, document: dict):
        created = copy.deepcopy(document)
        created["_rev"] = "1-created"
        self.documents[created["_id"]] = created
        return copy.deepcopy(created)

    def delete_doc(self, item_id: str):
        self.documents.pop(item_id)
        return {"ok": True}

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


def live_example_documents() -> dict[str, dict]:
    return {
        "task-wash-dishes-id": {
            "_id": "task-wash-dishes-id",
            "_rev": "1-wash",
            "db": "Tasks",
            "title": "Wash the dishes",
            "day": "2026-08-08",
            "firstScheduled": "2026-08-01",
            "updatedAt": 100,
        },
        "task-dinner-id": {
            "_id": "task-dinner-id",
            "_rev": "1-dinner",
            "db": "Tasks",
            "title": "Eat dinner with Jacob",
            "parentId": "unassigned",
            "updatedAt": 200,
        },
        "duplicate-task-id": {
            "_id": "duplicate-task-id",
            "_rev": "1-duplicate",
            "db": "Tasks",
            "title": "Study chapter 3",
            "updatedAt": 1_786_221_000_123,
        },
        "people-category-id": {
            "_id": "people-category-id",
            "db": "Categories",
            "type": "category",
            "title": "People",
            "parentId": "root",
        },
    }


def test_main_help_leads_with_safety_contract() -> None:
    result = runner.invoke(app, ["--help"])
    normalized = " ".join(result.stdout.split())
    assert result.exit_code == 0
    assert "separates reviewed Marvin mutations from explicitly bounded automation" in normalized
    assert "has enabled the local unattended policy" in normalized


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "marvin-pilot 1.0.1"


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


def test_validate_live_collects_all_errors_as_json(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path)
    documents = live_example_documents()
    documents["task-wash-dishes-id"]["title"] = "Changed wash title"
    documents["duplicate-task-id"]["updatedAt"] = 999
    client = ReadOnlyCliMarvinClient(documents)
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)

    result = runner.invoke(app, ["validate", str(path), "--live", "--json"])

    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert payload["errors"] == 2
    assert payload["warnings"] == 0
    assert [item["operationIndex"] for item in payload["diagnostics"]] == [1, 4]
    assert payload["diagnostics"][0]["expected"] == "Wash the dishes"
    assert payload["diagnostics"][0]["found"] == "Changed wash title"
    assert "Live validation" in result.stderr
    assert all(client.reads.count(item_id) == 1 for item_id in set(client.reads))
    assert client.closed


@pytest.mark.parametrize(
    "selector",
    [
        ["--from-index", "4"],
        ["--from-operation", "trash-duplicate-math-task"],
        ["--target", "duplicate-task-id"],
    ],
)
def test_validate_live_can_select_a_late_operation(
    isolated_app_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: list[str],
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path)
    client = ReadOnlyCliMarvinClient(live_example_documents())
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)

    result = runner.invoke(app, ["validate", str(path), "--live", *selector])

    assert result.exit_code == 0
    assert "Selection: 1 of 4 operation(s); processed 1" in result.stdout
    assert client.reads == ["duplicate-task-id"]
    assert client.closed


def test_validate_live_selectors_are_explicit_and_live_only(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path)
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("invalid selectors must fail before credentials"),
    )

    offline = runner.invoke(app, ["validate", str(path), "--from-index", "2"])
    combined = runner.invoke(
        app,
        ["validate", str(path), "--live", "--from-index", "2", "--target", "duplicate-task-id"],
    )
    out_of_range = runner.invoke(app, ["validate", str(path), "--live", "--from-index", "5"])

    assert offline.exit_code == 2
    assert "require --live" in offline.stderr
    assert combined.exit_code == 2
    assert "use only one" in combined.stderr
    assert out_of_range.exit_code == 2
    assert "between 1 and 4" in out_of_range.stderr


def test_describe_is_offline_and_readable(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    write_plan(path)
    result = runner.invoke(app, ["describe", str(path)])
    assert result.exit_code == 0
    assert 'UPDATE "Wash the dishes"' in result.stdout
    assert "Totals: 1 create, 2 updates, 1 trash" in result.stdout


def test_describe_supports_markdown_json_and_create_only_output(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    write_plan(path)
    markdown_path = tmp_path / "plan.md"
    markdown = runner.invoke(
        app,
        ["describe", str(path), "--format", "markdown", "--output", str(markdown_path)],
    )
    json_result = runner.invoke(app, ["describe", str(path), "--format", "json"])

    assert markdown.exit_code == 0
    assert markdown_path.read_text(encoding="utf-8").startswith(
        "# Refocus today on math and make the dinner task concrete."
    )
    payload = json.loads(json_result.stdout)
    assert payload["kind"] == "plan"
    assert payload["operations"][0]["operationId"] == "reschedule-wash-dishes"
    refused = runner.invoke(
        app,
        ["describe", str(path), "--format", "markdown", "--output", str(markdown_path)],
    )
    assert refused.exit_code == 2
    assert "refusing to overwrite" in refused.stderr


def test_plan_set_applies_reports_status_and_reverts_in_reverse_order(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = {"userId": "123456", "email": "pilot@example.com"}
    for index, task_id in enumerate(("task-one", "task-two"), start=1):
        write_plan(
            isolated_app_dirs / f"phase-{index}.json",
            {
                "schemaVersion": 1,
                "planId": (
                    f"{index}{index}{index}{index}{index}{index}{index}{index}"
                    "-1111-4111-8111-111111111111"
                ),
                "createdAt": "2026-09-04T12:00:00-07:00",
                "summary": f"Rename phase {index}.",
                "expectedAccount": account,
                "operations": [
                    {
                        "operationId": f"rename-{task_id}",
                        "action": "update",
                        "target": {"type": "task", "id": task_id, "title": f"Old {index}"},
                        "before": {"title": f"Old {index}"},
                        "after": {"title": f"New {index}"},
                        "reason": "Exercise dependency-ordered plan-set execution.",
                    }
                ],
            },
        )
    manifest = isolated_app_dirs / "cleanup.plan-set.json"
    manifest.write_text(
        json.dumps(
            {
                "planSetVersion": 1,
                "planSetId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "summary": "Apply two phases together.",
                "expectedAccount": account,
                "plans": [
                    {"path": "phase-1.json"},
                    {"path": "phase-2.json", "dependsOn": ["phase-1.json"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    client = MultiCliMarvinClient(
        {
            "task-one": {"_id": "task-one", "_rev": "1-a", "db": "Tasks", "title": "Old 1"},
            "task-two": {"_id": "task-two", "_rev": "1-b", "db": "Tasks", "title": "Old 2"},
        }
    )
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    monkeypatch.setattr(cli_module, "confirm_apply", lambda _count: True)
    monkeypatch.setattr(cli_module, "confirm_revert", lambda _count: True)

    applied = runner.invoke(app, ["apply", str(manifest)])
    applied_titles = [client.documents[item]["title"] for item in ("task-one", "task-two")]
    status = runner.invoke(app, ["history", "status", str(manifest), "--json"])
    reverted = runner.invoke(app, ["revert", str(manifest)])
    reverted_status = runner.invoke(app, ["history", "status", str(manifest), "--json"])

    assert applied.exit_code == 0, applied.output
    assert applied_titles == ["New 1", "New 2"]
    assert [client.documents[item]["title"] for item in ("task-one", "task-two")] == [
        "Old 1",
        "Old 2",
    ]
    assert json.loads(status.stdout)["results"][0]["status"] == "applied"
    assert reverted.exit_code == 0, reverted.output
    assert json.loads(reverted_status.stdout)["results"][0]["status"] == "reverted"


def test_visualize_preloads_without_credentials_and_opens_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plan.json"
    write_plan(path)
    calls: dict[str, object] = {}

    class FakeServer:
        url = "http://127.0.0.1:1234/session/"

        def __init__(self, *, preloaded_plan, source_name, hierarchy, **kwargs):
            calls["plan"] = preloaded_plan
            calls["source_name"] = source_name
            calls["hierarchy"] = hierarchy
            calls.update(kwargs)

        def serve_forever(self):
            calls["served"] = True

        def shutdown(self):
            calls["shutdown"] = True

    monkeypatch.setattr(cli_module, "VisualizerServer", FakeServer)
    monkeypatch.setattr(cli_module.webbrowser, "open", lambda url: calls.setdefault("url", url))
    monkeypatch.setattr(
        cli_module,
        "_load_config_or_fail",
        lambda: pytest.fail("visualize must not load config or credentials"),
    )
    result = runner.invoke(app, ["visualize", str(path)])
    assert result.exit_code == 0
    assert calls["source_name"] == "plan.json"
    assert calls["hierarchy"] is None
    assert calls["url"] == FakeServer.url
    assert calls["served"] is True
    assert calls["shutdown"] is True
    assert "Preview only" in result.stdout
    assert "visible state(s) omit typed paths" in result.stdout
    assert "--backup BACKUP.json.lzma" in result.stdout
    assert "No Marvin credential" in result.stdout


def test_visualize_no_open_and_missing_path_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plan.json"
    write_plan(path)
    opened: list[str] = []

    class FakeServer:
        url = "http://127.0.0.1:1234/session/"

        def __init__(self, **_kwargs):
            pass

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(cli_module, "VisualizerServer", FakeServer)
    monkeypatch.setattr(cli_module.webbrowser, "open", opened.append)
    result = runner.invoke(app, ["visualize", str(path), "--no-open"])
    missing = runner.invoke(app, ["visualize", str(tmp_path / "missing.json"), "--no-open"])
    assert result.exit_code == 0
    assert opened == []
    assert missing.exit_code == 2
    assert "not a regular file" in missing.stderr


def test_visualize_loads_optional_backup_hierarchy_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.json"
    backup_path = tmp_path / "backup.json"
    write_plan(plan_path)
    backup_path.write_text(
        json.dumps(
            [
                {
                    "_id": "home",
                    "db": "Categories",
                    "type": "category",
                    "title": "Home",
                    "parentId": "root",
                }
            ]
        ),
        encoding="utf-8",
    )
    calls: dict[str, object] = {}

    class FakeServer:
        url = "http://127.0.0.1:1234/session/"

        def __init__(self, *, preloaded_plan, source_name, hierarchy, **kwargs):
            calls["plan"] = preloaded_plan
            calls["source_name"] = source_name
            calls["hierarchy"] = hierarchy
            calls.update(kwargs)

        def serve_forever(self):
            calls["served"] = True

        def shutdown(self):
            calls["shutdown"] = True

    monkeypatch.setattr(cli_module, "VisualizerServer", FakeServer)
    monkeypatch.setattr(
        cli_module,
        "_load_config_or_fail",
        lambda: pytest.fail("visualize must not load config or credentials"),
    )

    result = runner.invoke(
        app,
        ["visualize", str(plan_path), "--backup", str(backup_path), "--no-open"],
    )

    assert result.exit_code == 0
    hierarchy = calls["hierarchy"]
    assert hierarchy.nodes["home"].title == "Home"
    assert "Hierarchy: 1 active item(s) loaded locally from the backup." in result.stdout
    assert "omit typed paths" not in result.stdout
    assert "No Marvin credential or API connection" in result.stdout


def _write_applied_receipt(path: Path, plan_dict: dict) -> ReceiptV1:
    plan = parse_plan_bytes(json.dumps(plan_dict).encode())
    operations = []
    for operation in plan.operations:
        before_document = None
        if operation.target.id == "task-wash-dishes-id":
            before_document = {
                "_id": operation.target.id,
                "db": "Tasks",
                "title": operation.target.title,
                "parentId": "receipt-parent",
            }
        operations.append(
            ReceiptOperationV1(
                operationId=operation.operationId,
                action=operation.action,
                targetId=operation.target.id,
                targetType=operation.target.type,
                targetTitle=getattr(operation.target, "title", None),
                status="applied",
                beforeDocument=before_document,
            )
        )
    receipt = ReceiptV1(
        receiptId="11111111-1111-4111-8111-111111111111",
        kind="apply",
        status="applied",
        startedAt="2026-08-30T12:00:00Z",
        endedAt="2026-08-30T12:01:00Z",
        cliVersion=__version__,
        sourcePlan=json.loads(json.dumps(plan_dict)),
        sourcePlanText=json.dumps(plan_dict),
        planId=plan.planId,
        planDigest=plan_digest(plan),
        apiBaseHost="https://marvin.test",
        operations=operations,
    )
    path.write_bytes(receipt_file_bytes(receipt))
    return receipt


def test_visualize_verifies_exact_applied_receipt_and_marks_view_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.json"
    receipt_path = tmp_path / "receipt.json"
    write_plan(plan_path)
    receipt = _write_applied_receipt(receipt_path, EXAMPLE_PLAN)
    calls: dict[str, object] = {}

    class FakeServer:
        url = "http://127.0.0.1:1234/session/"

        def __init__(self, **kwargs):
            calls.update(kwargs)

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(cli_module, "VisualizerServer", FakeServer)

    result = runner.invoke(
        app,
        ["visualize", str(plan_path), "--receipt", str(receipt_path), "--no-open"],
    )

    assert result.exit_code == 0
    assert calls["review_state"] == "applied"
    assert calls["receipt_id"] == receipt.receiptId
    assert calls["hierarchy_sources"] == ("verified apply receipt",)
    assert calls["hierarchy"].nodes["task-wash-dishes-id"].parent_id == "receipt-parent"
    assert "Applied plan — verified receipt" in result.stdout


def test_visualize_rejects_receipt_for_a_different_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.json"
    receipt_path = tmp_path / "receipt.json"
    changed = copy.deepcopy(EXAMPLE_PLAN)
    changed["summary"] = "A different exact plan."
    write_plan(plan_path, changed)
    _write_applied_receipt(receipt_path, EXAMPLE_PLAN)
    monkeypatch.setattr(
        cli_module,
        "VisualizerServer",
        lambda **_kwargs: pytest.fail("mismatch must fail before opening a server"),
    )

    result = runner.invoke(
        app,
        ["visualize", str(plan_path), "--receipt", str(receipt_path), "--no-open"],
    )

    assert result.exit_code == 3
    assert "does not exactly match" in result.stderr


def test_visualize_projects_repeatable_prerequisite_plans_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_path = tmp_path / "plan.json"
    context_path = tmp_path / "phase-one.json"
    write_plan(plan_path)
    project_id = "22222222-2222-4222-8222-222222222222"
    write_plan(
        context_path,
        {
            "schemaVersion": 1,
            "planId": "33333333-3333-4333-8333-333333333333",
            "createdAt": "2026-08-30T12:00:00-07:00",
            "summary": "Create a prerequisite project.",
            "operations": [
                {
                    "operationId": "create-project",
                    "action": "create",
                    "target": {"type": "project", "id": project_id},
                    "reason": "Provide hierarchy context for the next phase.",
                    "after": {"title": "Earlier phase project"},
                }
            ],
        },
    )
    calls: dict[str, object] = {}

    class FakeServer:
        url = "http://127.0.0.1:1234/session/"

        def __init__(self, **kwargs):
            calls.update(kwargs)

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(cli_module, "VisualizerServer", FakeServer)

    result = runner.invoke(
        app,
        [
            "visualize",
            str(plan_path),
            "--context-plan",
            str(context_path),
            "--no-open",
        ],
    )

    assert result.exit_code == 0
    assert calls["hierarchy"].nodes[project_id].title == "Earlier phase project"
    assert calls["hierarchy_sources"] == ("prerequisite plan projection",)


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


def test_doctor_checks_full_access_credential_without_mutation(
    isolated_app_dirs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = CliMarvinClient({})
    key_file = tmp_path / "full-token.txt"
    seen: dict[str, object] = {}

    def client_from_config(config, supplied_key_file):
        seen["mode"] = config.credential_mode
        seen["key_file"] = supplied_key_file
        return client

    monkeypatch.setattr(cli_module, "_build_client", client_from_config)
    result = runner.invoke(
        app,
        ["doctor", "--full-access-key-file", str(key_file)],
    )

    assert result.exit_code == 0
    assert "Marvin Pilot Doctor" in result.stdout
    assert "Configuration" in result.stdout
    assert "keyring credential mode" in result.stdout
    assert "Full-access token loaded securely" in result.stdout
    assert "pilot@example.com" in result.stdout
    assert "123456" in result.stdout
    assert "GET https://marvin.test/api/me" in result.stdout
    assert "200 OK" in result.stdout
    assert "All checks passed" in result.stdout
    assert "no Marvin data was changed" in result.stdout
    assert seen == {"mode": "keyring", "key_file": key_file}
    assert client.connection_checks == 1
    assert client.mutations == 0
    assert client.closed is True


@pytest.mark.parametrize(
    ("error", "exit_code", "http_description", "message"),
    [
        (
            CredentialError(
                "Amazing Marvin rejected the full-access credential (HTTP 401)",
                http_status_code=401,
            ),
            4,
            "401 Unauthorized",
            "Amazing Marvin rejected",
        ),
        (
            RemoteError("GET doc timed out"),
            8,
            "No HTTP response",
            "GET doc timed out",
        ),
        (RemoteError("missing", http_status_code=404), 8, "404 Not Found", "missing"),
        (
            RemoteError("rate limited", http_status_code=429),
            8,
            "429 Too Many Requests",
            "rate limited",
        ),
        (
            RemoteError("server failed", http_status_code=500),
            8,
            "500 Internal Server Error",
            "server failed",
        ),
    ],
)
def test_doctor_reports_connection_failure_and_closes_client(
    isolated_app_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    exit_code: int,
    http_description: str,
    message: str,
) -> None:
    class RejectingClient(CliMarvinClient):
        def check_connection(self) -> ConnectionCheck:
            raise error

    client = RejectingClient({})
    monkeypatch.setattr(cli_module, "_build_client", lambda *_args: client)
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == exit_code
    assert "Doctor found a problem" in result.stderr
    assert http_description in result.stderr
    assert message in result.stderr
    assert "no Marvin data was changed" in result.stderr
    assert client.mutations == 0
    assert client.closed is True


def test_doctor_report_uses_terminal_colors() -> None:
    output = io.StringIO()
    color_console = Console(
        file=output,
        force_terminal=True,
        color_system="standard",
        width=100,
    )
    cli_module._render_doctor_report(
        [("ok", "HTTP status", "200 OK"), ("warn", "Retry", "429 Too Many Requests")],
        success=True,
        target_console=color_console,
    )

    rendered = output.getvalue()
    assert "\x1b[" in rendered
    assert "200 OK" in rendered
    assert "429 Too Many Requests" in rendered


def test_doctor_report_is_cp1252_safe_for_legacy_windows() -> None:
    output = io.BytesIO()
    text_output = io.TextIOWrapper(output, encoding="cp1252", errors="strict")
    legacy_console = Console(file=text_output, force_terminal=False, width=100)

    cli_module._render_doctor_report(
        [("ok", "Account", "pilot@example.com"), ("fail", "HTTP status", "500 Error")],
        success=False,
        target_console=legacy_console,
    )
    text_output.flush()

    rendered = output.getvalue().decode("cp1252")
    assert "PASS" in rendered
    assert "FAIL" in rendered
    assert "Marvin Pilot Doctor" in rendered


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
    assert "recovery is through `marvin-pilot revert`" in result.stdout
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


def test_config_max_operations_round_trip_and_clamps_warning(
    isolated_app_dirs: Path,
) -> None:
    result = runner.invoke(app, ["config", "set-max-operations", "25"])
    show = runner.invoke(app, ["config", "show"])
    config = config_module.load_config(
        isolated_app_dirs / "roaming" / "marvin-pilot" / "config.toml"
    )
    assert result.exit_code == 0
    assert "Maximum operations: 25" in show.stdout
    assert config.max_operations == 25
    assert config.large_plan_warning_operations == 25


def test_config_unattended_enable_pins_verified_account(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = CliMarvinClient({})
    confirmations: list[tuple[str, int]] = []
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    monkeypatch.setattr(
        cli_module,
        "confirm_unattended_enable",
        lambda email, limit: confirmations.append((email, limit)) or True,
    )

    result = runner.invoke(
        app,
        ["config", "unattended", "enable", "--max-impact", "7"],
    )
    shown = runner.invoke(app, ["config", "unattended", "show"])

    assert result.exit_code == 0
    assert confirmations == [("pilot@example.com", 7)]
    assert client.connection_checks == 1
    assert client.closed
    assert "Verified account: pilot@example.com" in result.stdout
    assert "Enabled: yes" in shown.stdout
    assert "Maximum impact: 7" in shown.stdout
    assert "Account user ID: 123456" in shown.stdout

    disabled = runner.invoke(app, ["config", "unattended", "disable"])
    shown_after = runner.invoke(app, ["config", "unattended", "show"])
    assert disabled.exit_code == 0
    assert "Enabled: no" in shown_after.stdout
    assert "Account user ID: not pinned" in shown_after.stdout


def test_config_unattended_decline_changes_nothing(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = CliMarvinClient({})
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    monkeypatch.setattr(cli_module, "confirm_unattended_enable", lambda *_args: False)
    result = runner.invoke(app, ["config", "unattended", "enable"])
    config = config_module.load_config(
        isolated_app_dirs / "roaming" / "marvin-pilot" / "config.toml"
    )
    assert result.exit_code == 6
    assert config.unattended_enabled is False


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
    assert "Preflight" in applied.stderr
    assert "Apply" in applied.stderr
    assert "1/1" in applied.stderr
    assert "reschedule-wash-dishes" in applied.stderr
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
    assert "Revert preflight" in reverted.stderr
    assert "Revert" in reverted.stderr
    assert "1/1" in reverted.stderr
    assert "reschedule-wash-dishes" in reverted.stderr
    assert client.document["day"] == "2026-08-08"
    assert client.closed
    assert received_key_files == [key_file, key_file]
    assert "revert  reverted" in runner.invoke(app, ["history", "list"]).stdout


def test_history_audit_writes_review_only_completion_day_repair(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed_at = "2026-09-04T08:15:00-07:00"
    done_at = int(datetime.fromisoformat(completed_at).timestamp() * 1_000)
    operation = {
        "operationId": "complete-history",
        "action": "complete",
        "target": {
            "type": "task",
            "id": "task-complete",
            "title": "Archive the records",
        },
        "completedAt": completed_at,
    }
    receipt = ReceiptV1(
        receiptId="receipt-completion-history",
        kind="apply",
        status="applied",
        startedAt="2026-09-04T15:15:00Z",
        endedAt="2026-09-04T15:16:00Z",
        cliVersion="test",
        sourcePlan={"operations": [operation]},
        sourcePlanText=json.dumps({"operations": [operation]}),
        planId="11111111-1111-4111-8111-111111111111",
        planDigest="sha256:test",
        apiBaseHost="https://marvin.test",
        accountUserId="123456",
        accountEmail="pilot@example.com",
        operations=[
            ReceiptOperationV1(
                operationId="complete-history",
                action="complete",
                targetId="task-complete",
                targetType="task",
                targetTitle="Archive the records",
                status="applied",
                plannedAfter={"done": True, "completedAt": completed_at},
                afterFields={
                    "done": {"present": True, "value": True},
                    "doneAt": {"present": True, "value": done_at},
                },
            )
        ],
    )
    receipt_path = isolated_app_dirs / "old-receipt.json"
    receipt_path.write_bytes(receipt_file_bytes(receipt))
    repair_path = isolated_app_dirs / "completion-repair.json"
    client = CliMarvinClient(
        {
            "_id": "task-complete",
            "_rev": "2-completed",
            "db": "Tasks",
            "title": "Archive the records",
            "done": True,
            "doneAt": done_at,
            "day": "unassigned",
            "updatedAt": 1234,
        }
    )
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)

    result = runner.invoke(
        app,
        [
            "history",
            "audit",
            str(receipt_path),
            "--live",
            "--repair-plan",
            str(repair_path),
        ],
    )

    assert result.exit_code == 0
    assert "completion history missing-day" in result.stdout
    assert f"Repair plan: {repair_path}" in result.stdout
    repair, _raw = cli_module.load_plan(repair_path)
    assert repair.expectedAccount.userId == "123456"
    assert repair.operations[0].before.scheduledDate is None
    assert repair.operations[0].after.scheduledDate == "2026-09-04"
    assert client.mutations == 0
    assert client.closed


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


def test_history_audit_writes_review_only_project_timestamp_repair(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed_at = "2026-07-18T18:30:00-07:00"
    operation = {
        "operationId": "complete-project-history",
        "action": "complete",
        "target": {
            "type": "project",
            "id": "project-complete",
            "title": "Finished project",
        },
        "completedAt": completed_at,
    }
    receipt = ReceiptV1(
        receiptId="receipt-project-completion-history",
        kind="apply",
        status="applied",
        startedAt="2026-07-19T01:30:00Z",
        endedAt="2026-07-19T01:31:00Z",
        cliVersion="test",
        sourcePlan={"operations": [operation]},
        sourcePlanText=json.dumps({"operations": [operation]}),
        planId="22222222-2222-4222-8222-222222222222",
        planDigest="sha256:project-test",
        apiBaseHost="https://marvin.test",
        accountUserId="123456",
        accountEmail="pilot@example.com",
        operations=[
            ReceiptOperationV1(
                operationId="complete-project-history",
                action="complete",
                targetId="project-complete",
                targetType="project",
                targetTitle="Finished project",
                status="applied",
                plannedAfter={"done": True, "completedAt": completed_at},
                afterFields={
                    "done": {"present": True, "value": True},
                    "doneDate": {"present": True, "value": "2026-07-18"},
                },
            )
        ],
    )
    receipt_path = isolated_app_dirs / "old-project-receipt.json"
    receipt_path.write_bytes(receipt_file_bytes(receipt))
    repair_path = isolated_app_dirs / "project-completion-repair.json"
    client = CliMarvinClient(
        {
            "_id": "project-complete",
            "_rev": "2-completed",
            "db": "Categories",
            "type": "project",
            "title": "Finished project",
            "done": True,
            "doneDate": "2026-07-18",
            "day": "unassigned",
            "updatedAt": 4321,
        }
    )
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)

    result = runner.invoke(
        app,
        [
            "history",
            "audit",
            str(receipt_path),
            "--live",
            "--repair-plan",
            str(repair_path),
        ],
    )

    assert result.exit_code == 0
    assert "completion history missing-timestamp" in result.stdout
    repair, _raw = cli_module.load_plan(repair_path)
    repair_operation = repair.operations[0]
    assert repair_operation.action == "complete"
    assert repair_operation.target.type == "project"
    assert repair_operation.completedAt == completed_at
    assert repair_operation.repairHistory is True
    assert repair_operation.expectedUpdatedAt == 4321
    assert client.mutations == 0
    assert client.closed


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
    assert "apply declined; no Marvin changes were made" in result.stderr
    assert client.mutations == 0
    assert runner.invoke(app, ["history", "list"]).stdout.strip() == "No receipts."


def test_apply_preflight_reports_all_live_errors_before_approval(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path)
    documents = live_example_documents()
    documents["task-wash-dishes-id"]["title"] = "Changed wash title"
    documents["duplicate-task-id"]["updatedAt"] = 999
    client = ReadOnlyCliMarvinClient(documents)
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    monkeypatch.setattr(
        cli_module,
        "confirm_apply",
        lambda _count: pytest.fail("a failing preflight must not request approval"),
    )

    result = runner.invoke(app, ["apply", str(path)])

    assert result.exit_code == 5
    assert "live preflight found 2 errors" in result.stderr
    assert "reschedule-wash-dishes" in result.stderr
    assert "trash-duplicate-math-task" in result.stderr
    assert all(client.reads.count(item_id) == 1 for item_id in set(client.reads))
    assert client.closed
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


def test_apply_unattended_requires_human_enabled_policy_before_credentials(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path, one_operation_plan())
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("disabled unattended mode must fail before credentials"),
    )
    result = runner.invoke(app, ["apply", str(path), "--unattended"])
    assert result.exit_code == 3
    assert "unattended apply is disabled" in result.stderr
    assert "config unattended enable --max-impact 10" in result.stderr


def test_apply_unattended_rejects_minimum_impact_before_credentials(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path, EXAMPLE_PLAN)
    cli_module.save_config(
        config_module.AppConfig(
            unattended_enabled=True,
            unattended_max_impact=3,
            unattended_account_user_id="123456",
        )
    )
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("minimum impact must fail before credentials"),
    )
    result = runner.invoke(app, ["apply", str(path), "--unattended"])
    assert result.exit_code == 3
    assert "at least 4 impact" in result.stderr
    assert "maximum is 3" in result.stderr


def test_apply_unattended_blocks_project_trash_before_credentials(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "createdAt": "2026-09-04T12:00:00-07:00",
        "summary": "Exercise unattended project deletion boundary.",
        "operations": [
            {
                "operationId": "trash-project",
                "action": "trash",
                "target": {
                    "type": "project",
                    "id": "project-1",
                    "title": "Important project",
                },
                "reason": "Exercise the safety boundary.",
            }
        ],
    }
    path = isolated_app_dirs / "plan.json"
    write_plan(path, plan)
    cli_module.save_config(
        config_module.AppConfig(
            unattended_enabled=True,
            unattended_account_user_id="123456",
        )
    )
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("blocked container operation must fail before credentials"),
    )
    result = runner.invoke(app, ["apply", str(path), "--unattended"])
    assert result.exit_code == 3
    assert "trashes a container" in result.stderr
    assert "interactive review" in result.stderr


def test_apply_unattended_accepts_piped_plan_without_terminal_or_prompt(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_module.AppConfig(
        unattended_enabled=True,
        unattended_max_impact=1,
        unattended_account_user_id="123456",
    )
    cli_module.save_config(config)
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
    monkeypatch.setattr(
        cli_module,
        "confirm_apply",
        lambda _count: pytest.fail("unattended mode must not prompt"),
    )
    monkeypatch.setattr(
        cli_module,
        "require_controlling_terminal",
        lambda: pytest.fail("unattended mode must not require a terminal"),
    )

    plan = one_operation_plan()
    plan["expectedAccount"] = {"userId": "123456", "email": "pilot@example.com"}
    result = runner.invoke(
        app,
        ["apply", "-", "--unattended"],
        input=json.dumps(plan),
    )

    assert result.exit_code == 0
    assert "UNATTENDED APPLY AUTHORIZED" in result.stdout
    assert "impact 1/1" in result.stdout
    assert "Receipt:" in result.stdout
    assert client.connection_checks == 3
    assert client.document["day"] == "2026-08-09"
    assert client.mutations == 1


def test_apply_unattended_rejects_account_mismatch_before_plan_reads(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    plan = one_operation_plan()
    plan["expectedAccount"] = {"userId": "999999", "email": "other@example.com"}
    write_plan(path, plan)
    cli_module.save_config(
        config_module.AppConfig(
            unattended_enabled=True,
            unattended_account_user_id="999999",
        )
    )
    client = CliMarvinClient({})
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    result = runner.invoke(app, ["apply", str(path), "--unattended"])
    assert result.exit_code == 3
    assert "account mismatch" in result.stderr
    assert client.connection_checks == 1
    assert client.mutations == 0
    assert client.closed


def test_apply_unattended_counts_deleted_task_subtasks_in_impact(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = {
        "schemaVersion": 1,
        "planId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "createdAt": "2026-09-04T12:00:00-07:00",
        "summary": "Exercise unattended checklist impact.",
        "expectedAccount": {"userId": "123456", "email": "pilot@example.com"},
        "operations": [
            {
                "operationId": "trash-task",
                "action": "trash",
                "target": {"type": "task", "id": "task-1", "title": "Checklist"},
                "reason": "Remove a disposable checklist.",
            }
        ],
    }
    path = isolated_app_dirs / "plan.json"
    write_plan(path, plan)
    cli_module.save_config(
        config_module.AppConfig(
            unattended_enabled=True,
            unattended_max_impact=1,
            unattended_account_user_id="123456",
        )
    )
    client = CliMarvinClient(
        {
            "_id": "task-1",
            "_rev": "1-task",
            "db": "Tasks",
            "title": "Checklist",
            "done": False,
            "updatedAt": 1,
            "subtasks": {"step-1": {"_id": "step-1", "title": "One"}},
        }
    )
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    result = runner.invoke(app, ["apply", str(path), "--unattended"])
    assert result.exit_code == 3
    assert "plan impact is 2; configured unattended maximum is 1" in result.stderr
    assert client.mutations == 0


def test_small_reviewed_apply_shows_unattended_setup_tip(
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
    normalized = " ".join(result.stdout.split())
    assert "This small plan has impact 1" in normalized
    assert "config unattended" in normalized
    assert "enable --max-impact 10" in normalized


def test_apply_rejects_yes_with_unattended_before_credentials(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path, one_operation_plan())
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("conflicting options must fail before credentials"),
    )
    result = runner.invoke(app, ["apply", str(path), "--yes", "--unattended"])
    assert result.exit_code == 2
    assert "either --yes or --unattended" in result.stderr


def test_apply_rejects_inline_token_and_documents_reviewed_yes(
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
    help_result = runner.invoke(app, ["apply", "--help"])
    assert inline.exit_code == 2
    assert "--full-access-key-file" in help_result.stdout
    assert "--yes" in help_result.stdout
    assert "-y" in help_result.stdout
    assert "interactive controlling terminal" in " ".join(help_result.stdout.split())


def test_apply_yes_skips_prompt_after_terminal_and_preflight_checks(
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
    terminal_checks: list[bool] = []
    monkeypatch.setattr(cli_module, "_client_from_config", lambda *_args: client)
    monkeypatch.setattr(
        cli_module,
        "require_controlling_terminal",
        lambda: terminal_checks.append(True),
    )
    monkeypatch.setattr(
        cli_module,
        "confirm_apply",
        lambda _count: pytest.fail("--yes must skip the approval question"),
    )

    result = runner.invoke(app, ["apply", str(path), "--yes"])

    assert result.exit_code == 0
    assert terminal_checks == [True]
    assert "PROMPT SKIPPED" in result.stdout
    assert "Explicit --yes/-y supplied" in result.stdout
    assert client.document["day"] == "2026-08-09"
    assert client.mutations == 1


def test_apply_yes_rejects_missing_controlling_terminal_before_credentials(
    isolated_app_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = isolated_app_dirs / "plan.json"
    write_plan(path, one_operation_plan())

    def reject_terminal() -> None:
        raise cli_module.PlanSyntaxError(
            "apply/revert requires an interactive controlling terminal"
        )

    monkeypatch.setattr(cli_module, "require_controlling_terminal", reject_terminal)
    monkeypatch.setattr(
        cli_module,
        "_client_from_config",
        lambda *_args: pytest.fail("terminal check must fail before credentials"),
    )
    result = runner.invoke(app, ["apply", str(path), "-y"])
    assert result.exit_code == 2
    assert "requires an interactive controlling terminal" in result.stderr


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
