from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from marvin_pilot.errors import LivePreconditionError, PlanSemanticError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.history import HistoryStore
from marvin_pilot.history_status import audit_receipt_live, plan_history_status
from marvin_pilot.models.receipt_v1 import ReceiptOperationV1, ReceiptV1
from marvin_pilot.plan_io import parse_plan_bytes


class AuditReader:
    def __init__(self, documents: dict[str, dict | None]) -> None:
        self.documents = documents

    def check_connection(self):
        return SimpleNamespace(account_user_id="123456", account_email="pilot@example.com")

    def get_doc(self, item_id: str):
        return self.documents.get(item_id)


def receipt() -> ReceiptV1:
    return ReceiptV1(
        receiptId="receipt-1",
        kind="apply",
        status="applied",
        startedAt="2026-09-04T12:00:00Z",
        endedAt="2026-09-04T12:01:00Z",
        cliVersion="test",
        sourcePlan={},
        sourcePlanText="{}",
        planId="11111111-1111-4111-8111-111111111111",
        planDigest="sha256:test",
        apiBaseHost="https://marvin.test",
        accountUserId="123456",
        accountEmail="pilot@example.com",
        operations=[
            ReceiptOperationV1(
                operationId="rename",
                action="update",
                targetId="task-1",
                targetTitle="Task",
                status="applied",
                afterFields={"title": {"present": True, "value": "New title"}},
            ),
            ReceiptOperationV1(
                operationId="trash",
                action="trash",
                targetId="task-2",
                targetTitle="Old task",
                status="applied",
            ),
        ],
    )


def test_live_audit_distinguishes_matches_changed_later_and_deletion() -> None:
    result = audit_receipt_live(
        receipt(),
        AuditReader(
            {
                "task-1": {"title": "Different", "updatedAt": 1_788_566_500_000},
                "task-2": None,
            }
        ),
    )
    assert [item["state"] for item in result] == ["changed-later", "matches"]


def test_local_status_reports_never_applied(tmp_path: Path) -> None:
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    status = plan_history_status(plan, HistoryStore(tmp_path))
    assert status.status == "never-applied"
    assert status.receipt_path is None


class StatusHistory:
    def __init__(self, stored: ReceiptV1, claims: set[str] | None = None) -> None:
        self.stored = stored
        self.claims = claims or set()

    def list_paths(self):
        return [Path("receipt.json")]

    def load(self, _path: Path):
        return self.stored

    def find_revert_claims(self, **_kwargs):
        return {operation_id: Path("revert.json") for operation_id in self.claims}


@pytest.mark.parametrize(
    ("receipt_status", "operation_status", "claims", "expected"),
    [
        ("pending", "not-started", set(), "ambiguous"),
        ("applied", "verifying", set(), "ambiguous"),
        ("partial", "applied", set(), "partial"),
        ("failed", "failed", set(), "partial"),
        ("reverting", "applied", set(), "changed"),
        ("applied", "applied", set(), "applied"),
        ("applied", "applied", {"rename"}, "selectively-reverted"),
        ("applied", "applied", {"rename", "trash"}, "reverted"),
    ],
)
def test_local_status_classifies_receipt_and_revert_states(
    receipt_status: str,
    operation_status: str,
    claims: set[str],
    expected: str,
) -> None:
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    stored = receipt().model_copy(deep=True)
    stored.planId = plan.planId
    stored.status = receipt_status
    for operation in stored.operations:
        operation.status = operation_status

    status = plan_history_status(plan, StatusHistory(stored, claims))

    assert status.status == expected
    assert status.receipt_path == "receipt.json"


def test_live_audit_reports_all_nonmatching_states() -> None:
    stored = receipt().model_copy(deep=True)
    stored.endedAt = "not-a-timestamp"
    stored.operations[0].status = "failed"
    stored.operations.extend(
        [
            ReceiptOperationV1(
                operationId="missing",
                action="update",
                targetId="task-missing",
                targetTitle="Missing",
                status="applied",
                afterFields={"title": {"present": True, "value": "Expected"}},
            ),
            ReceiptOperationV1(
                operationId="exact",
                action="update",
                targetId="task-exact",
                targetTitle="Exact",
                status="applied",
                afterFields={"title": {"present": True, "value": "Expected"}},
            ),
            ReceiptOperationV1(
                operationId="mismatch",
                action="update",
                targetId="task-mismatch",
                targetTitle="Mismatch",
                status="applied",
                afterFields={"title": {"present": True, "value": "Expected"}},
            ),
        ]
    )
    result = audit_receipt_live(
        stored,
        AuditReader(
            {
                "task-2": {"title": "Restored"},
                "task-missing": None,
                "task-exact": {"title": "Expected"},
                "task-mismatch": {"title": "Different", "updatedAt": 9_999_999_999_999},
            }
        ),
    )
    assert [item["state"] for item in result] == [
        "mismatch",
        "present",
        "missing",
        "matches",
        "mismatch",
    ]


def test_live_audit_rejects_revert_receipts_and_account_mismatch() -> None:
    revert = receipt().model_copy(update={"kind": "revert"})
    with pytest.raises(PlanSemanticError, match="apply receipt"):
        audit_receipt_live(revert, AuditReader({}))

    stored = receipt().model_copy(update={"accountUserId": "different"})
    with pytest.raises(LivePreconditionError, match="account mismatch"):
        audit_receipt_live(stored, AuditReader({}))
