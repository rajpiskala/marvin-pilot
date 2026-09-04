"""Dependency-ordered plan-set manifests and parent execution receipts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, StrictStr, ValidationError, field_validator, model_validator

from marvin_pilot.atomic import atomic_write_bytes, ensure_private_directory
from marvin_pilot.errors import (
    HistoryError,
    LivePreconditionError,
    PlanSemanticError,
    PlanSyntaxError,
)
from marvin_pilot.models.plan_v1 import ChangePlanV1, ClosedModel, ExpectedAccount
from marvin_pilot.plan_io import MAX_PLAN_BYTES, load_plan, plan_digest


class PlanSetEntry(ClosedModel):
    path: StrictStr
    dependsOn: list[StrictStr] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("plans[].path must not be empty")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("plans[].path must be a safe relative path")
        return value


class PlanSetV1(ClosedModel):
    planSetVersion: Literal[1]
    planSetId: StrictStr
    summary: StrictStr
    expectedAccount: ExpectedAccount
    plans: list[PlanSetEntry] = Field(min_length=1, max_length=100)

    @field_validator("planSetId")
    @classmethod
    def validate_id(cls, value: str) -> str:
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("planSetId must be a UUID") from exc

    @model_validator(mode="after")
    def validate_graph(self) -> PlanSetV1:
        paths = [entry.path for entry in self.plans]
        if len(paths) != len(set(paths)):
            raise ValueError("plans[].path values must be unique")
        prior: set[str] = set()
        for entry in self.plans:
            missing = [dependency for dependency in entry.dependsOn if dependency not in prior]
            if missing:
                raise ValueError(
                    f"plan {entry.path!r} depends on missing or later plan(s): "
                    + ", ".join(missing)
                )
            prior.add(entry.path)
        return self


@dataclass(frozen=True, slots=True)
class LoadedPlanSetEntry:
    entry: PlanSetEntry
    path: Path
    plan: ChangePlanV1
    raw: bytes


@dataclass(frozen=True, slots=True)
class LoadedPlanSet:
    manifest: PlanSetV1
    path: Path
    raw: bytes
    plans: tuple[LoadedPlanSetEntry, ...]


class PlanSetChildReceipt(ClosedModel):
    planPath: StrictStr
    planId: StrictStr
    planDigest: StrictStr
    receiptPath: StrictStr
    status: StrictStr


class PlanSetReceiptV1(ClosedModel):
    planSetReceiptVersion: Literal[1] = 1
    receiptId: StrictStr
    kind: Literal["apply", "revert"] = "apply"
    planSetId: StrictStr
    status: Literal["applying", "applied", "partial", "reverting", "reverted"]
    startedAt: StrictStr
    endedAt: StrictStr | None = None
    sourceManifest: dict[str, Any]
    sourceManifestDigest: StrictStr
    sourcePlans: list[dict[str, StrictStr]]
    accountUserId: StrictStr
    accountEmail: StrictStr
    sourcePlanSetReceiptId: StrictStr | None = None
    sourcePlanSetReceiptPath: StrictStr | None = None
    children: list[PlanSetChildReceipt]
    receiptHash: StrictStr = ""


def parse_plan_set_bytes(raw: bytes) -> PlanSetV1:
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanSyntaxError(f"plan-set manifest exceeds the {MAX_PLAN_BYTES}-byte limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanSyntaxError(f"plan-set manifest is not strict UTF-8 JSON: {exc}") from exc
    try:
        return PlanSetV1.model_validate(value)
    except ValidationError as exc:
        raise PlanSyntaxError(f"invalid Marvin Pilot plan-set manifest: {exc}") from exc


def load_plan_set(path: Path) -> LoadedPlanSet:
    if not path.is_file():
        raise PlanSyntaxError(f"plan-set path is not a regular file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanSyntaxError(f"could not read plan-set {path}: {exc}") from exc
    manifest = parse_plan_set_bytes(raw)
    root = path.resolve().parent
    plans: list[LoadedPlanSetEntry] = []
    operation_ids: set[str] = set()
    for entry in manifest.plans:
        plan_path = (root / entry.path).resolve()
        if plan_path.parent != root and root not in plan_path.parents:
            raise PlanSyntaxError(f"plan-set entry escapes its directory: {entry.path}")
        plan, plan_raw = load_plan(plan_path)
        if plan.expectedAccount != manifest.expectedAccount:
            raise PlanSemanticError(
                f"plan {entry.path!r} expectedAccount must exactly match the plan set"
            )
        duplicates = operation_ids & {operation.operationId for operation in plan.operations}
        if duplicates:
            raise PlanSemanticError(
                "operation IDs must be unique across a plan set: " + ", ".join(sorted(duplicates))
            )
        operation_ids.update(operation.operationId for operation in plan.operations)
        plans.append(LoadedPlanSetEntry(entry=entry, path=plan_path, plan=plan, raw=plan_raw))
    return LoadedPlanSet(manifest=manifest, path=path.resolve(), raw=raw, plans=tuple(plans))


def plan_set_digest(loaded: LoadedPlanSet) -> str:
    value = {
        "manifest": loaded.manifest.model_dump(mode="json"),
        "plans": [
            {"path": item.entry.path, "planId": item.plan.planId, "digest": plan_digest(item.plan)}
            for item in loaded.plans
        ],
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def combined_preview_plan(loaded: LoadedPlanSet) -> ChangePlanV1:
    """Build a display-only aggregate without changing any child artifact."""

    return ChangePlanV1.model_construct(
        schema_=None,
        schemaVersion=1,
        planId=loaded.manifest.planSetId,
        createdAt=max(item.plan.createdAt for item in loaded.plans),
        summary=loaded.manifest.summary,
        expectedAccount=loaded.manifest.expectedAccount,
        reviewDisplay=None,
        operations=[operation for item in loaded.plans for operation in item.plan.operations],
    )


def new_plan_set_receipt(loaded: LoadedPlanSet) -> PlanSetReceiptV1:
    return PlanSetReceiptV1(
        receiptId=str(uuid4()),
        planSetId=loaded.manifest.planSetId,
        status="applying",
        startedAt=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        sourceManifest=loaded.manifest.model_dump(mode="json"),
        sourceManifestDigest=plan_set_digest(loaded),
        sourcePlans=[
            {"path": item.entry.path, "planId": item.plan.planId, "digest": plan_digest(item.plan)}
            for item in loaded.plans
        ],
        accountUserId=loaded.manifest.expectedAccount.userId,
        accountEmail=loaded.manifest.expectedAccount.email,
        children=[],
    )


def new_plan_set_revert_receipt(source: PlanSetReceiptV1, source_path: Path) -> PlanSetReceiptV1:
    return PlanSetReceiptV1(
        receiptId=str(uuid4()),
        kind="revert",
        planSetId=source.planSetId,
        status="reverting",
        startedAt=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        sourceManifest=source.sourceManifest,
        sourceManifestDigest=source.sourceManifestDigest,
        sourcePlans=source.sourcePlans,
        accountUserId=source.accountUserId,
        accountEmail=source.accountEmail,
        sourcePlanSetReceiptId=source.receiptId,
        sourcePlanSetReceiptPath=str(source_path.resolve()),
        children=[],
    )


def _receipt_path(history_root: Path, receipt: PlanSetReceiptV1) -> Path:
    directory = history_root / "plan-sets"
    ensure_private_directory(directory)
    return directory / f"{receipt.planSetId}--{receipt.receiptId}.json"


def persist_plan_set_receipt(history_root: Path, receipt: PlanSetReceiptV1) -> Path:
    path = _receipt_path(history_root, receipt)
    receipt.receiptHash = _plan_set_receipt_hash(receipt)
    atomic_write_bytes(
        path,
        (json.dumps(receipt.model_dump(mode="json", exclude_none=True), indent=2) + "\n").encode(),
    )
    return path


def load_plan_set_receipt(path: Path) -> PlanSetReceiptV1:
    try:
        receipt = PlanSetReceiptV1.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise HistoryError(f"invalid plan-set receipt {path}: {exc}") from exc
    if receipt.receiptHash != _plan_set_receipt_hash(receipt):
        raise HistoryError(f"plan-set receipt integrity check failed: {path}")
    return receipt


def _plan_set_receipt_hash(receipt: PlanSetReceiptV1) -> str:
    value = receipt.model_dump(mode="json", exclude_none=True, exclude={"receiptHash"})
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def find_latest_plan_set_receipt(
    history_root: Path, plan_set_id: str, *, manifest_digest_value: str | None = None
) -> tuple[Path, PlanSetReceiptV1] | None:
    directory = history_root / "plan-sets"
    if not directory.exists():
        return None
    matches: list[tuple[Path, PlanSetReceiptV1]] = []
    for path in directory.glob(f"{plan_set_id}--*.json"):
        receipt = load_plan_set_receipt(path)
        if receipt.kind != "apply":
            continue
        if (
            manifest_digest_value is not None
            and receipt.sourceManifestDigest != manifest_digest_value
        ):
            continue
        matches.append((path, receipt))
    return max(matches, key=lambda item: item[1].startedAt) if matches else None


def find_latest_plan_set_revert(
    history_root: Path, source_receipt_id: str
) -> tuple[Path, PlanSetReceiptV1] | None:
    directory = history_root / "plan-sets"
    if not directory.exists():
        return None
    matches = []
    for path in directory.glob("*.json"):
        receipt = load_plan_set_receipt(path)
        if receipt.kind == "revert" and receipt.sourcePlanSetReceiptId == source_receipt_id:
            matches.append((path, receipt))
    return max(matches, key=lambda item: item[1].startedAt) if matches else None


class ProjectedReader:
    """Share one read cache and projected state across plan-set phase validation."""

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.documents: dict[str, dict[str, Any] | None] = {}
        self.labels: list[dict[str, Any]] | None = None

    def check_connection(self) -> Any:
        return self.reader.check_connection()

    @property
    def api_base_host(self) -> str:
        return self.reader.api_base_host

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        if item_id not in self.documents:
            self.documents[item_id] = self.reader.get_doc(item_id)
        value = self.documents[item_id]
        return deepcopy(value) if value is not None else None

    def get_labels(self) -> list[dict[str, Any]]:
        if self.labels is None:
            self.labels = self.reader.get_labels()
        return deepcopy(self.labels)

    def project(self, target_id: str, document: dict[str, Any] | None) -> None:
        self.documents[target_id] = deepcopy(document)


def verify_manifest_account(loaded: LoadedPlanSet, reader: Any) -> Any:
    account = reader.check_connection()
    expected = loaded.manifest.expectedAccount
    if (
        account.account_user_id != expected.userId
        or account.account_email.casefold() != expected.email.casefold()
    ):
        raise LivePreconditionError(
            f"plan-set account mismatch: expected {expected.email} ({expected.userId}), "
            f"connected {account.account_email} ({account.account_user_id})"
        )
    return account
