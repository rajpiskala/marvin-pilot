"""Versioned, integrity-checked audit receipt models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ReceiptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


OperationStatus = Literal[
    "not-started",
    "checking",
    "sending",
    "verifying",
    "applied",
    "failed",
    "unknown",
    "stale",
    "reverting",
    "reverted",
    "already-reverted",
]

ReceiptStatus = Literal[
    "pending",
    "applying",
    "applied",
    "partial",
    "failed",
    "pending-revert",
    "reverting",
    "reverted",
    "partial-revert",
    "failed-revert",
]


class RequestRecord(ReceiptModel):
    endpoint: str
    payload: dict[str, Any]


class ReceiptRecurrenceV1(ReceiptModel):
    scope: Literal["occurrence"] = "occurrence"
    seriesId: str
    seriesTitle: str
    scheduledDate: str


class ReceiptCompletionHistoryV1(ReceiptModel):
    expectedDay: str
    documentDayVerified: bool = False
    serverHistoryStatus: Literal["not-checked", "visible", "missing"] = "not-checked"
    detail: str


class ReceiptOperationV1(ReceiptModel):
    operationId: str
    action: Literal["update", "create", "trash", "complete"]
    targetId: str
    targetType: Literal["task", "project", "category", "recurringTask"] = "task"
    targetTitle: str | None = None
    recurrence: ReceiptRecurrenceV1 | None = None
    status: OperationStatus = "not-started"
    applyIndex: int | None = None
    startedAt: str | None = None
    endedAt: str | None = None
    plannedBefore: dict[str, Any] | None = None
    plannedAfter: dict[str, Any] | None = None
    beforeFields: dict[str, dict[str, Any]] = Field(default_factory=dict)
    desiredFields: dict[str, Any] = Field(default_factory=dict)
    afterFields: dict[str, dict[str, Any]] = Field(default_factory=dict)
    beforeDocument: dict[str, Any] | None = None
    afterDocument: dict[str, Any] | None = None
    completionHistory: ReceiptCompletionHistoryV1 | None = None
    request: RequestRecord | None = None
    outcome: str | None = None
    error: str | None = None


class ReceiptV1(ReceiptModel):
    receiptSchemaVersion: Literal[1] = 1
    receiptId: str
    kind: Literal["apply", "revert"]
    status: ReceiptStatus
    startedAt: str
    endedAt: str | None = None
    cliVersion: str
    sourcePlan: dict[str, Any]
    sourcePlanText: str
    planId: str
    planDigest: str
    apiBaseHost: str
    accountUserId: str | None = None
    accountEmail: str | None = None
    sourceApplyReceiptId: str | None = None
    sourceApplyReceiptPath: str | None = None
    selectedOperationIds: list[str] = Field(default_factory=list)
    operations: list[ReceiptOperationV1]
    receiptHash: str = ""
