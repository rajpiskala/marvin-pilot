from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from marvin_pilot.errors import LivePreconditionError, PlanSemanticError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.history import HistoryStore
from marvin_pilot.history_status import (
    audit_receipt_live,
    build_completion_day_repair_plan,
    plan_history_status,
)
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


def completion_receipt() -> tuple[ReceiptV1, int]:
    completed_at = "2026-09-04T08:15:00-07:00"
    done_at = int(datetime.fromisoformat(completed_at).timestamp() * 1_000)
    stored = receipt().model_copy(deep=True)
    stored.sourcePlan = {
        "operations": [
            {
                "operationId": "complete-history",
                "action": "complete",
                "target": {
                    "type": "task",
                    "id": "task-complete",
                    "title": "Archive the records",
                },
                "completedAt": completed_at,
            }
        ]
    }
    stored.operations = [
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
    ]
    return stored, done_at


def test_live_audit_detects_and_builds_locked_completion_day_repair() -> None:
    stored, done_at = completion_receipt()
    rows = audit_receipt_live(
        stored,
        AuditReader(
            {
                "task-complete": {
                    "_id": "task-complete",
                    "db": "Tasks",
                    "title": "Archive the records",
                    "done": True,
                    "doneAt": done_at,
                    "day": "unassigned",
                    "updatedAt": 1234,
                }
            }
        ),
    )

    assert rows[0]["state"] == "mismatch"
    assert rows[0]["completionHistory"] == {
        "state": "missing-day",
        "expectedDay": "2026-09-04",
        "actualDay": None,
        "plannedCompletedAt": "2026-09-04T08:15:00-07:00",
        "doneAtMatches": True,
        "discoverability": "unverified",
        "repairable": True,
    }

    plan = build_completion_day_repair_plan(
        rows,
        expected_account={"userId": "123456", "email": "pilot@example.com"},
        created_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    operation = plan.operations[0]
    assert operation.action == "update"
    assert operation.before.scheduledDate is None
    assert operation.after.scheduledDate == "2026-09-04"
    assert operation.display is not None
    assert operation.display.existingCompletedAt == "2026-09-04T08:15:00-07:00"
    assert operation.expectedUpdatedAt == 1234


def test_completion_audit_refuses_unsafe_timestamp_repair() -> None:
    stored, done_at = completion_receipt()
    rows = audit_receipt_live(
        stored,
        AuditReader(
            {
                "task-complete": {
                    "title": "Archive the records",
                    "done": True,
                    "doneAt": done_at + 1,
                    "day": "2026-09-01",
                    "updatedAt": 1234,
                }
            }
        ),
    )

    assert rows[0]["completionHistory"]["state"] == "completion-timestamp-changed"
    assert rows[0]["completionHistory"]["repairable"] is False
    with pytest.raises(PlanSemanticError, match="no confirmed completion-day repairs"):
        build_completion_day_repair_plan(
            rows,
            expected_account={"userId": "123456", "email": "pilot@example.com"},
        )


def test_recurring_completion_repair_locks_the_current_occurrence_day() -> None:
    stored, done_at = completion_receipt()
    stored.sourcePlan["operations"][0]["target"]["recurrence"] = {
        "scope": "occurrence",
        "seriesId": "series-1",
        "seriesTitle": "Archive records weekly",
        "scheduledDate": "2026-09-01",
    }
    rows = audit_receipt_live(
        stored,
        AuditReader(
            {
                "task-complete": {
                    "title": "Archive the records",
                    "done": True,
                    "doneAt": done_at,
                    "day": "2026-09-02",
                    "updatedAt": 1234,
                }
            }
        ),
    )

    assert rows[0]["completionHistory"]["state"] == "wrong-day"
    plan = build_completion_day_repair_plan(
        rows,
        expected_account={"userId": "123456", "email": "pilot@example.com"},
    )
    operation = plan.operations[0]
    assert operation.target.recurrence.scheduledDate == "2026-09-02"
    assert operation.before.scheduledDate == "2026-09-02"
    assert operation.after.scheduledDate == "2026-09-04"
