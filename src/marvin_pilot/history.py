"""Durable, atomic receipt history and duplicate-apply detection."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from marvin_pilot import __version__
from marvin_pilot.atomic import atomic_write_bytes, ensure_private_directory, exclusive_write_bytes
from marvin_pilot.errors import HistoryError
from marvin_pilot.models.plan_v1 import CreateOperation, UpdateOperation
from marvin_pilot.models.receipt_v1 import (
    ReceiptOperationV1,
    ReceiptStatus,
    ReceiptV1,
    RequestRecord,
)
from marvin_pilot.plan_io import canonical_plan_bytes, plan_digest
from marvin_pilot.preflight import PreflightResult

_RECEIPT_FILENAME_RE = re.compile(
    r"^(?:pending-revert|partial-revert|failed-revert|pending|applied|partial|failed|reverted)-"
    r"(?P<timestamp>\d{8}T\d{6}\.\d{3}Z)--"
)


@dataclass(slots=True)
class ReceiptHandle:
    receipt: ReceiptV1
    path: Path


def utc_now() -> datetime:
    return datetime.now(UTC)


def rfc3339_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _filename_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%f")[:19] + "Z"


def _canonical_receipt_bytes(receipt: ReceiptV1, *, include_hash: bool) -> bytes:
    excluded = set() if include_hash else {"receiptHash"}
    value = receipt.model_dump(mode="json", exclude_none=True, exclude=excluded)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HistoryError(f"receipt contains a non-JSON value: {exc}") from exc


def receipt_hash(receipt: ReceiptV1) -> str:
    return (
        "sha256:"
        + hashlib.sha256(_canonical_receipt_bytes(receipt, include_hash=False)).hexdigest()
    )


def receipt_file_bytes(receipt: ReceiptV1) -> bytes:
    receipt.receiptHash = receipt_hash(receipt)
    value = receipt.model_dump(mode="json", exclude_none=True)
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _terminal_prefix(status: ReceiptStatus) -> str:
    prefixes = {
        "applied": "applied",
        "partial": "partial",
        "failed": "failed",
        "reverted": "reverted",
        "partial-revert": "partial-revert",
        "failed-revert": "failed-revert",
    }
    try:
        return prefixes[status]
    except KeyError as exc:
        raise HistoryError(f"status {status!r} is not terminal") from exc


class HistoryStore:
    """Own receipt filenames, hashing, persistence, and history lookup."""

    def __init__(self, root: Path, *, now: Callable[[], datetime] = utc_now) -> None:
        self.root = root
        self._now = now

    def ensure_writable(self) -> None:
        """Prove the history directory can durably create a file before live writes."""

        ensure_private_directory(self.root)
        probe = self.root / f".write-check-{uuid4()}.tmp"
        try:
            exclusive_write_bytes(probe, b"marvin-pilot-history-write-check\n")
            probe.unlink()
        except OSError as exc:
            raise HistoryError(f"history directory is not writable: {self.root}: {exc}") from exc

    def _pending_path(self, receipt: ReceiptV1) -> Path:
        prefix = "pending" if receipt.kind == "apply" else "pending-revert"
        return self.root / (
            f"{prefix}-{_filename_timestamp(receipt.startedAt)}--{receipt.receiptId}.json"
        )

    def _terminal_path(self, receipt: ReceiptV1) -> Path:
        prefix = _terminal_prefix(receipt.status)
        return self.root / (
            f"{prefix}-{_filename_timestamp(receipt.startedAt)}--{receipt.receiptId}.json"
        )

    def begin_apply(
        self,
        preflight: PreflightResult,
        source_plan_bytes: bytes,
        *,
        api_base_host: str,
    ) -> ReceiptHandle:
        """Exclusively create the pending apply journal before the first mutation."""

        started_at = rfc3339_utc(self._now())
        operations = []
        for checked in preflight.operations:
            operation = checked.operation
            planned_before = None
            planned_after = None
            if isinstance(operation, UpdateOperation):
                planned_before = operation.before.model_dump(exclude_unset=True, mode="json")
                planned_after = operation.after.model_dump(exclude_unset=True, mode="json")
            elif isinstance(operation, CreateOperation):
                planned_after = operation.after.model_dump(exclude_unset=True, mode="json")
            operations.append(
                ReceiptOperationV1(
                    operationId=operation.operationId,
                    action=operation.action,
                    targetId=operation.target.id,
                    targetTitle=getattr(operation.target, "title", None),
                    plannedBefore=planned_before,
                    plannedAfter=planned_after,
                    beforeFields=checked.compiled.before_fields,
                    desiredFields=checked.compiled.desired_fields,
                    request=RequestRecord(
                        endpoint=checked.compiled.endpoint,
                        payload=checked.compiled.payload,
                    ),
                )
            )
        receipt = ReceiptV1(
            receiptId=str(uuid4()),
            kind="apply",
            status="pending",
            startedAt=started_at,
            cliVersion=__version__,
            sourcePlan=json.loads(canonical_plan_bytes(preflight.plan)),
            sourcePlanText=source_plan_bytes.decode("utf-8"),
            planId=preflight.plan.planId,
            planDigest=plan_digest(preflight.plan),
            apiBaseHost=api_base_host,
            operations=operations,
        )
        path = self._pending_path(receipt)
        exclusive_write_bytes(path, receipt_file_bytes(receipt))
        return ReceiptHandle(receipt=receipt, path=path)

    def begin_revert(
        self,
        source_receipt: ReceiptV1,
        source_receipt_path: Path,
        operations: list[ReceiptOperationV1],
        *,
        api_base_host: str,
    ) -> ReceiptHandle:
        """Exclusively create a pending revert journal before the first mutation."""

        started_at = rfc3339_utc(self._now())
        receipt = ReceiptV1(
            receiptId=str(uuid4()),
            kind="revert",
            status="pending-revert",
            startedAt=started_at,
            cliVersion=__version__,
            sourcePlan=source_receipt.sourcePlan,
            sourcePlanText=source_receipt.sourcePlanText,
            planId=source_receipt.planId,
            planDigest=source_receipt.planDigest,
            apiBaseHost=api_base_host,
            sourceApplyReceiptId=source_receipt.receiptId,
            sourceApplyReceiptPath=str(source_receipt_path.resolve()),
            selectedOperationIds=[operation.operationId for operation in operations],
            operations=operations,
        )
        path = self._pending_path(receipt)
        exclusive_write_bytes(path, receipt_file_bytes(receipt))
        return ReceiptHandle(receipt=receipt, path=path)

    def persist(self, handle: ReceiptHandle) -> None:
        atomic_write_bytes(handle.path, receipt_file_bytes(handle.receipt))

    def finalize(self, handle: ReceiptHandle, status: ReceiptStatus) -> Path:
        """Persist a terminal state and rename the pending journal accordingly."""

        handle.receipt.status = status
        handle.receipt.endedAt = rfc3339_utc(self._now())
        self.persist(handle)
        destination = self._terminal_path(handle.receipt)
        if destination.exists():
            raise HistoryError(f"refusing to overwrite receipt: {destination}")
        try:
            os.replace(handle.path, destination)
        except OSError as exc:
            raise HistoryError(f"could not finalize receipt {handle.path}: {exc}") from exc
        handle.path = destination
        return destination

    def load(self, path: Path) -> ReceiptV1:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            receipt = ReceiptV1.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise HistoryError(f"invalid receipt {path}: {exc}") from exc
        expected = receipt_hash(receipt)
        if not hmac.compare_digest(receipt.receiptHash, expected):
            raise HistoryError(f"receipt integrity check failed: {path}")
        return receipt

    def list_paths(self) -> list[Path]:
        if not self.root.exists():
            return []
        try:
            paths = list(self.root.glob("*.json"))
            return sorted(
                paths,
                key=lambda path: (
                    (
                        match.group("timestamp")
                        if (match := _RECEIPT_FILENAME_RE.match(path.name))
                        else ""
                    ),
                    path.name,
                ),
                reverse=True,
            )
        except OSError as exc:
            raise HistoryError(f"could not list history directory {self.root}: {exc}") from exc

    def find_duplicate(self, *, plan_id: str, digest: str) -> tuple[Path, ReceiptV1] | None:
        """Find an active/successful/partial apply that makes reapplication unsafe."""

        guarded_statuses = {"pending", "applying", "applied", "partial"}
        for path in self.list_paths():
            receipt = self.load(path)
            if (
                receipt.kind == "apply"
                and receipt.status in guarded_statuses
                and (receipt.planId == plan_id or receipt.planDigest == digest)
            ):
                return path, receipt
        return None

    def find_revert_claims(
        self,
        *,
        source_apply_receipt_id: str,
        operation_ids: set[str],
    ) -> dict[str, Path]:
        """Find operations already reverted or held by an unresolved revert journal."""

        claims: dict[str, Path] = {}
        guarded_statuses = {
            "pending-revert",
            "reverting",
            "reverted",
            "partial-revert",
        }
        for path in self.list_paths():
            receipt = self.load(path)
            if (
                receipt.kind != "revert"
                or receipt.sourceApplyReceiptId != source_apply_receipt_id
                or receipt.status not in guarded_statuses
            ):
                continue
            for operation_id in receipt.selectedOperationIds:
                if operation_id in operation_ids:
                    claims.setdefault(operation_id, path)
        return claims

    def latest(self) -> tuple[Path, ReceiptV1] | None:
        paths = self.list_paths()
        if not paths:
            return None
        return paths[0], self.load(paths[0])
