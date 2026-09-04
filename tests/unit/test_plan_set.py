from __future__ import annotations

import json
from pathlib import Path

import pytest

from marvin_pilot.errors import HistoryError, PlanSemanticError, PlanSyntaxError
from marvin_pilot.plan_set import (
    combined_preview_plan,
    load_plan_set,
    load_plan_set_receipt,
    new_plan_set_receipt,
    new_plan_set_revert_receipt,
    persist_plan_set_receipt,
)

ACCOUNT = {"userId": "123456", "email": "pilot@example.com"}


def _plan(plan_id: str, operation_id: str) -> dict:
    return {
        "schemaVersion": 1,
        "planId": plan_id,
        "createdAt": "2026-09-04T12:00:00-07:00",
        "summary": operation_id,
        "expectedAccount": ACCOUNT,
        "operations": [
            {
                "operationId": operation_id,
                "action": "trash",
                "target": {"type": "task", "id": operation_id, "title": operation_id},
                "reason": "Test the manifest.",
            }
        ],
    }


def _write_set(tmp_path: Path, *, dependency: str = "one.json") -> Path:
    (tmp_path / "one.json").write_text(
        json.dumps(_plan("11111111-1111-4111-8111-111111111111", "one")), encoding="utf-8"
    )
    (tmp_path / "two.json").write_text(
        json.dumps(_plan("22222222-2222-4222-8222-222222222222", "two")), encoding="utf-8"
    )
    manifest = tmp_path / "cleanup.plan-set.json"
    manifest.write_text(
        json.dumps(
            {
                "planSetVersion": 1,
                "planSetId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "summary": "Two phases.",
                "expectedAccount": ACCOUNT,
                "plans": [
                    {"path": "one.json"},
                    {"path": "two.json", "dependsOn": [dependency]},
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_load_plan_set_resolves_order_and_builds_combined_preview(tmp_path: Path) -> None:
    loaded = load_plan_set(_write_set(tmp_path))
    assert [item.entry.path for item in loaded.plans] == ["one.json", "two.json"]
    assert [operation.operationId for operation in combined_preview_plan(loaded).operations] == [
        "one",
        "two",
    ]


def test_plan_set_rejects_later_dependency_and_path_escape(tmp_path: Path) -> None:
    with pytest.raises(PlanSyntaxError, match="missing or later"):
        load_plan_set(_write_set(tmp_path, dependency="later.json"))

    manifest = json.loads(_write_set(tmp_path).read_text(encoding="utf-8"))
    manifest["plans"][0]["path"] = "../outside.json"
    path = tmp_path / "unsafe.plan-set.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PlanSyntaxError, match="safe relative path"):
        load_plan_set(path)


def test_plan_set_requires_exact_account_match(tmp_path: Path) -> None:
    path = _write_set(tmp_path)
    plan = json.loads((tmp_path / "two.json").read_text(encoding="utf-8"))
    plan["expectedAccount"]["userId"] = "999999"
    (tmp_path / "two.json").write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(PlanSemanticError, match="expectedAccount"):
        load_plan_set(path)


def test_plan_set_receipts_are_hashed_and_link_reverts(tmp_path: Path) -> None:
    loaded = load_plan_set(_write_set(tmp_path))
    applied = new_plan_set_receipt(loaded)
    applied.status = "applied"
    applied_path = persist_plan_set_receipt(tmp_path / "history", applied)
    restored = load_plan_set_receipt(applied_path)
    assert restored.receiptHash.startswith("sha256:")

    reverted = new_plan_set_revert_receipt(restored, applied_path)
    assert reverted.kind == "revert"
    assert reverted.sourcePlanSetReceiptId == restored.receiptId
    assert reverted.sourcePlanSetReceiptPath == str(applied_path.resolve())

    value = json.loads(applied_path.read_text(encoding="utf-8"))
    value["sourceManifest"]["summary"] = "tampered"
    applied_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(HistoryError, match="integrity"):
        load_plan_set_receipt(applied_path)
