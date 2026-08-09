"""Strict loading, semantic validation, canonicalization, and digesting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.models.plan_v1 import ChangePlanV1, UpdateOperation

MAX_PLAN_BYTES = 4 * 1024 * 1024
PLAN_MODELS = {1: ChangePlanV1}
SUPPORTED_SCHEMA_VERSIONS = tuple(PLAN_MODELS)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanSyntaxError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise PlanSyntaxError(f"invalid JSON numeric constant: {value}")


def parse_plan_bytes(raw: bytes) -> ChangePlanV1:
    """Parse untrusted UTF-8 JSON and enforce the closed v1 schema."""

    if len(raw) > MAX_PLAN_BYTES:
        raise PlanSyntaxError(f"plan exceeds the {MAX_PLAN_BYTES}-byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanSyntaxError("plan must be valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except PlanSyntaxError:
        raise
    except json.JSONDecodeError as exc:
        raise PlanSyntaxError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}; "
            "comments and trailing commas are not allowed"
        ) from exc
    if not isinstance(value, dict):
        raise PlanSyntaxError("the plan root must be a JSON object, not an array or scalar")
    schema_version = value.get("schemaVersion")
    if type(schema_version) is int and schema_version not in PLAN_MODELS:
        supported = ", ".join(str(version) for version in SUPPORTED_SCHEMA_VERSIONS)
        raise PlanSyntaxError(
            f"unsupported schemaVersion {schema_version}; this Marvin Pilot build supports: "
            f"{supported}. Upgrade Marvin Pilot before reviewing or applying this plan"
        )
    model = PLAN_MODELS.get(schema_version, ChangePlanV1)
    try:
        plan = model.model_validate(value)
    except ValidationError as exc:
        messages = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            messages.append(f"{location}: {error['msg']}")
        raise PlanSyntaxError("plan schema validation failed:\n- " + "\n- ".join(messages)) from exc
    validate_plan_semantics(plan)
    return plan


def validate_plan_semantics(plan: ChangePlanV1) -> None:
    """Validate relationships that JSON Schema cannot express clearly."""

    operation_ids: set[str] = set()
    target_ids: set[str] = set()
    prior_operation_ids: set[str] = set()

    for operation in plan.operations:
        if operation.operationId in operation_ids:
            raise PlanSemanticError(f"duplicate operationId: {operation.operationId!r}")
        operation_ids.add(operation.operationId)

        if operation.target.id in target_ids:
            raise PlanSemanticError(
                f"more than one operation targets task {operation.target.id!r}; "
                "coalesce the changes"
            )
        target_ids.add(operation.target.id)

        for dependency in operation.dependsOnOperations:
            if dependency not in prior_operation_ids:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} depends on {dependency!r}, "
                    "which must be an earlier operation"
                )

        if isinstance(operation, UpdateOperation):
            before_keys = operation.before.model_fields_set
            after_keys = operation.after.model_fields_set
            if before_keys != after_keys:
                missing_before = sorted(after_keys - before_keys)
                missing_after = sorted(before_keys - after_keys)
                detail = []
                if missing_before:
                    detail.append(f"missing from before: {', '.join(missing_before)}")
                if missing_after:
                    detail.append(f"missing from after: {', '.join(missing_after)}")
                raise PlanSemanticError(
                    f"update operation {operation.operationId!r} must use identical before/after "
                    f"field sets ({'; '.join(detail)})"
                )
            if not before_keys:
                raise PlanSemanticError(
                    f"update operation {operation.operationId!r} must change at least one field"
                )
            if "title" in after_keys and (
                operation.before.title is None or operation.after.title is None
            ):
                raise PlanSemanticError(
                    f"update operation {operation.operationId!r} cannot compare or clear "
                    "a null title"
                )
            before = operation.before.model_dump(exclude_unset=True, mode="json")
            after = operation.after.model_dump(exclude_unset=True, mode="json")
            if before == after:
                raise PlanSemanticError(
                    f"update operation {operation.operationId!r} does not change any values"
                )

        prior_operation_ids.add(operation.operationId)


def load_plan(path: Path) -> tuple[ChangePlanV1, bytes]:
    """Load a plan from a regular file."""

    try:
        if not path.is_file():
            raise PlanSyntaxError(f"plan path is not a regular file: {path}")
        size = path.stat().st_size
        if size > MAX_PLAN_BYTES:
            raise PlanSyntaxError(f"plan exceeds the {MAX_PLAN_BYTES}-byte limit")
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanSyntaxError(f"could not read plan {path}: {exc}") from exc
    return parse_plan_bytes(raw), raw


def canonical_plan_bytes(plan: ChangePlanV1) -> bytes:
    """Return deterministic UTF-8 JSON for digesting and receipt identity."""

    value = plan.model_dump(by_alias=True, exclude_unset=True, mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def plan_digest(plan: ChangePlanV1) -> str:
    """Return the canonical SHA-256 plan identity."""

    return "sha256:" + hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()
