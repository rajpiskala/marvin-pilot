"""Narrow, offline plan revisions. Never refresh locks or mutate Marvin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, TypeAdapter, ValidationError

from marvin_pilot.completion import completion_day_behavior, completion_local_date
from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.models.plan_v1 import ChangePlanV1, Operation
from marvin_pilot.plan_io import canonical_plan_bytes, parse_plan_bytes


class RevisionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operationId: StrictStr
    exclude: StrictBool = False
    title: StrictStr | None = None
    completedAt: StrictStr | None = None


def _reference_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {
            reference
            for key, child in value.items()
            for reference in (
                {child}
                if key in {"id", "beforeId", "afterId"} and isinstance(child, str)
                else _reference_ids(child)
            )
        }
    if isinstance(value, list):
        return set().union(*(_reference_ids(child) for child in value))
    return set()


def revise_plan(
    source: ChangePlanV1, edits: list[dict[str, Any]], *, created_at: str | None = None
) -> ChangePlanV1:
    """Return a new validated proposal, preserving baseline locks and explicit nulls."""

    if not edits or len(edits) > len(source.operations):
        raise PlanSemanticError("supply one or more edits, at most one per operation")
    try:
        parsed = [RevisionEdit.model_validate(edit) for edit in edits]
    except ValidationError as exc:
        raise PlanSyntaxError(f"invalid revision edit: {exc}") from exc
    by_id = {edit.operationId: edit for edit in parsed}
    if len(by_id) != len(parsed):
        raise PlanSemanticError("each operation may be edited only once")
    original = json.loads(canonical_plan_bytes(source))
    operations = original["operations"]
    known = {operation["operationId"] for operation in operations}
    if by_id.keys() - known:
        raise PlanSemanticError("unknown revision operation ID")
    excluded = {edit.operationId for edit in parsed if edit.exclude}
    if any(
        edit.exclude and (edit.title is not None or edit.completedAt is not None) for edit in parsed
    ):
        raise PlanSemanticError("an excluded operation cannot also be edited")
    excluded_targets = {
        operation["target"]["id"]
        for operation in operations
        if operation["operationId"] in excluded
    }
    for operation in operations:
        if operation["operationId"] in excluded:
            continue
        dependency = set(operation.get("dependsOnOperations", [])) & excluded
        references = _reference_ids(operation) & excluded_targets
        if dependency or references:
            raise PlanSemanticError(
                f"cannot exclude a dependency of {operation['operationId']!r}; "
                "exclude its dependent operations too, or ask the author to revise the plan"
            )
    revised = []
    projected_titles: dict[str, str] = {}
    changed_titles: dict[str, str] = {}
    for operation in operations:
        identifier = operation["operationId"]
        if identifier in excluded:
            continue
        edit = by_id.get(identifier)
        target_id = operation["target"]["id"]
        if target_id in projected_titles and operation["action"] != "create":
            operation["target"]["title"] = projected_titles[target_id]
            if "title" in operation.get("before", {}):
                operation["before"]["title"] = projected_titles[target_id]
        if edit is not None:
            if not edit.exclude and edit.title is None and edit.completedAt is None:
                raise PlanSemanticError("revision edit has no changes")
            if edit.title is not None:
                after = operation.get("after", {})
                if operation["action"] not in {"create", "update"} or "title" not in after:
                    raise PlanSemanticError("titles can be edited only on creates or title updates")
                if edit.title == after["title"]:
                    raise PlanSemanticError("title edit does not change the proposal")
                after["title"] = edit.title
                changed_titles[target_id] = edit.title
            if edit.completedAt is not None:
                if "completedAt" not in operation:
                    raise PlanSemanticError(
                        "completion time requires an existing completion action"
                    )
                if edit.completedAt == operation["completedAt"]:
                    raise PlanSemanticError("completion edit does not change the proposal")
                # Parse timestamp strictly before calculating its day (no naive/local fallback).
                temporary = dict(operation, completedAt=edit.completedAt)
                temporary.pop("completionDay", None)
                try:
                    TypeAdapter(Operation).validate_python(temporary)
                except ValidationError as exc:
                    raise PlanSyntaxError(f"invalid completion edit: {exc}") from exc
                operation["completedAt"] = edit.completedAt
                if "completionDay" in operation:
                    transition = operation["completionDay"]
                    transition["after"] = completion_local_date(edit.completedAt)
                    transition["behavior"] = completion_day_behavior(
                        transition["before"], transition["after"]
                    )
        title = operation.get("after", {}).get("title")
        if target_id in changed_titles and isinstance(title, str):
            projected_titles[target_id] = title
        revised.append(operation)
    for operation in revised:
        for node in operation.get("display", {}).get("afterPath") or []:
            if node["id"] in changed_titles:
                node["title"] = changed_titles[node["id"]]
        parent = operation.get("after", {}).get("parent")
        if isinstance(parent, dict) and parent["id"] in changed_titles:
            parent["title"] = changed_titles[parent["id"]]
    original.update(
        planId=str(uuid4()),
        createdAt=created_at or datetime.now(UTC).isoformat(),
        operations=revised,
    )
    return parse_plan_bytes(json.dumps(original).encode())
