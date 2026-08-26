"""Strict loading, semantic validation, canonicalization, and digesting."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.field_registry import compile_fields_for_target
from marvin_pilot.models.plan_v1 import (
    ChangePlanV1,
    CompleteOperation,
    CreateOperation,
    CreateRecurringTaskFields,
    CreateTaskFields,
    Operation,
    RecurringTaskFields,
    TaskFields,
    TrashOperation,
    UpdateOperation,
)

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
    prior_target_operations: dict[str, Operation] = {}
    first_target_operations: dict[str, Operation] = {}
    current_target_titles: dict[str, str] = {}
    prior_operation_ids: set[str] = set()
    prior_creates: dict[str, CreateOperation] = {}
    all_creates = {
        operation.target.id: operation
        for operation in plan.operations
        if isinstance(operation, CreateOperation)
    }
    operation_positions = {
        operation.operationId: position for position, operation in enumerate(plan.operations)
    }
    target_counts = Counter(operation.target.id for operation in plan.operations)
    trash_by_target = {
        operation.target.id: operation
        for operation in plan.operations
        if isinstance(operation, TrashOperation)
    }
    converted_sources: dict[str, str] = {}
    plan_created_at = datetime.fromisoformat(plan.createdAt.replace("Z", "+00:00"))
    display_types: dict[tuple[str, str], str] = {}
    display_titles: dict[tuple[str, str], str] = {}
    display_parents: dict[tuple[str, str], str | None] = {}
    display_orders: dict[tuple[str, str], int | None] = {}
    day_sections: dict[tuple[str, str], tuple[str, int]] = {}
    task_fields = set(TaskFields.model_fields)
    recurring_task_fields = set(RecurringTaskFields.model_fields)

    for operation in plan.operations:
        if operation.operationId in operation_ids:
            raise PlanSemanticError(f"duplicate operationId: {operation.operationId!r}")
        operation_ids.add(operation.operationId)

        prior_target = prior_target_operations.get(operation.target.id)
        if prior_target is not None:
            if isinstance(prior_target, TrashOperation):
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} targets item {operation.target.id!r} "
                    f"after terminal trash operation {prior_target.operationId!r}"
                )
            if isinstance(operation, CreateOperation):
                raise PlanSemanticError(
                    f"create operation {operation.operationId!r} targets item "
                    f"{operation.target.id!r} more than once"
                )
            if isinstance(first_target_operations[operation.target.id], CreateOperation):
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} repeats newly created target "
                    f"{operation.target.id!r}; coalesce its fields into the create operation"
                )
            if operation.target.type != prior_target.target.type:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} changes target "
                    f"{operation.target.id!r} from type {prior_target.target.type!r} to "
                    f"{operation.target.type!r}"
                )
            if prior_target.operationId not in operation.dependsOnOperations:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} repeats target "
                    f"{operation.target.id!r}; add immediate prior operation "
                    f"{prior_target.operationId!r} to dependsOnOperations"
                )
            expected_title = current_target_titles[operation.target.id]
            if operation.target.title != expected_title:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} chained target title is stale: "
                    f"expected {expected_title!r}, found {operation.target.title!r}"
                )
            if operation.expectedUpdatedAt is not None:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} follows another operation on target "
                    f"{operation.target.id!r}; only the first operation may set "
                    "expectedUpdatedAt"
                )
        elif isinstance(operation, CreateOperation):
            current_target_titles[operation.target.id] = operation.after.title
        else:
            current_target_titles[operation.target.id] = operation.target.title

        if operation.display is not None:
            if "existingCompletedAt" in operation.display.model_fields_set:
                if operation.display.existingCompletedAt is None:
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} has a null "
                        "display.existingCompletedAt; omit it when the completion is unknown"
                    )
                if not isinstance(operation, (UpdateOperation, TrashOperation)):
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} may use "
                        "display.existingCompletedAt only for update or trash"
                    )
                if operation.target.type != "task":
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} may use "
                        "display.existingCompletedAt only for an existing task"
                    )
            if isinstance(operation, CreateOperation):
                invalid = operation.display.model_fields_set & {
                    "beforePath",
                    "beforeOrder",
                    "beforeDaySection",
                }
                if invalid:
                    raise PlanSemanticError(
                        f"create operation {operation.operationId!r} has no before state; "
                        f"remove display metadata: {', '.join(sorted(invalid))}"
                    )
            if operation.action == "trash":
                invalid = operation.display.model_fields_set & {
                    "afterPath",
                    "afterOrder",
                    "afterDaySection",
                }
                if invalid:
                    raise PlanSemanticError(
                        f"trash operation {operation.operationId!r} has no active after state; "
                        f"remove display metadata: {', '.join(sorted(invalid))}"
                    )

            for side in ("before", "after"):
                side_exists = not (
                    (side == "before" and isinstance(operation, CreateOperation))
                    or (side == "after" and operation.action == "trash")
                )
                if not side_exists:
                    continue
                path_field = f"{side}Path"
                path = getattr(operation.display, path_field)
                if path_field in operation.display.model_fields_set and path is not None:
                    parent_id = None
                    for node in path:
                        identity_key = (side, node.id)
                        previous_type = display_types.setdefault(identity_key, node.type)
                        if previous_type != node.type:
                            raise PlanSemanticError(
                                f"display path item {node.id!r} is both {previous_type!r} and "
                                f"{node.type!r} on the {side} side"
                            )
                        previous_title = display_titles.setdefault(identity_key, node.title)
                        if previous_title != node.title:
                            raise PlanSemanticError(
                                f"display path item {node.id!r} has conflicting {side} titles"
                            )
                        previous_parent = display_parents.setdefault(identity_key, parent_id)
                        if previous_parent != parent_id:
                            raise PlanSemanticError(
                                f"display path item {node.id!r} has conflicting {side} parents"
                            )
                        previous_order = display_orders.setdefault(identity_key, node.order)
                        if previous_order != node.order:
                            raise PlanSemanticError(
                                f"display path item {node.id!r} has conflicting {side} order"
                            )
                        parent_id = node.id

                    target_key = (
                        side,
                        operation.target.id
                        if target_counts[operation.target.id] == 1
                        else f"{operation.target.id}:{operation.operationId}",
                    )
                    previous_type = display_types.setdefault(target_key, operation.target.type)
                    if previous_type != operation.target.type:
                        raise PlanSemanticError(
                            f"target {operation.target.id!r} conflicts with its {side} display "
                            "path type"
                        )
                    target_fields = getattr(operation, side, None)
                    target_title = getattr(operation.target, "title", None)
                    if (
                        target_fields is not None
                        and "title" in target_fields.model_fields_set
                        and target_fields.title is not None
                    ):
                        target_title = target_fields.title
                    if target_title is not None:
                        previous_title = display_titles.setdefault(target_key, target_title)
                        if previous_title != target_title:
                            raise PlanSemanticError(
                                f"target {operation.target.id!r} conflicts with its {side} "
                                "display path title"
                            )
                    previous_parent = display_parents.setdefault(target_key, parent_id)
                    if previous_parent != parent_id:
                        raise PlanSemanticError(
                            f"target {operation.target.id!r} has conflicting {side} display parents"
                        )

                    fields = getattr(operation, side, None)
                    if fields is not None and "parent" in fields.model_fields_set:
                        expected_parent = fields.parent.id if fields.parent is not None else None
                        if parent_id != expected_parent:
                            raise PlanSemanticError(
                                f"operation {operation.operationId!r} display.{path_field} does "
                                f"not end at its {side} parent"
                            )

                day_field = f"{side}DaySection"
                section = getattr(operation.display, day_field)
                if section is not None:
                    section_key = (side, section.key)
                    definition = (section.title, section.order)
                    previous = day_sections.setdefault(section_key, definition)
                    if previous != definition:
                        raise PlanSemanticError(
                            f"day section {section.key!r} has conflicting {side} display metadata"
                        )

        for dependency in operation.dependsOnOperations:
            if dependency not in prior_operation_ids:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} depends on {dependency!r}, "
                    "which must be an earlier operation"
                )

        if isinstance(operation, UpdateOperation):
            before_keys = operation.before.model_fields_set
            after_keys = operation.after.model_fields_set
            allowed_fields = (
                recurring_task_fields if operation.target.type == "recurringTask" else task_fields
            )
            unsupported_target_fields = sorted((before_keys | after_keys) - allowed_fields)
            if unsupported_target_fields:
                if operation.target.type == "recurringTask":
                    detail = "non-series recurringTask field(s)"
                else:
                    detail = "recurrence-series field(s)"
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} uses {detail} on a "
                    f"{operation.target.type}: " + ", ".join(unsupported_target_fields)
                )
            expected_model = (
                RecurringTaskFields if operation.target.type == "recurringTask" else TaskFields
            )
            for side, value in (("before", operation.before), ("after", operation.after)):
                try:
                    expected_model.model_validate(value.model_dump(exclude_unset=True, mode="json"))
                except ValidationError as exc:
                    location = ".".join(str(part) for part in exc.errors()[0]["loc"])
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} has invalid {side} fields for "
                        f"target type {operation.target.type!r} at {location}"
                    ) from exc
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
            semantic_before = compile_fields_for_target(operation.target.type, before)
            semantic_after = compile_fields_for_target(operation.target.type, after)
            if semantic_before == semantic_after:
                raise PlanSemanticError(
                    f"update operation {operation.operationId!r} does not change any values"
                )

        if (
            isinstance(operation, (UpdateOperation, CreateOperation))
            and operation.target.type != "recurringTask"
        ):
            after_subtasks = (
                operation.after.subtasks
                if "subtasks" in operation.after.model_fields_set
                and operation.after.subtasks is not None
                else []
            )
            before_subtask_ids: set[str] = set()
            if isinstance(operation, UpdateOperation):
                before_subtasks = (
                    operation.before.subtasks
                    if "subtasks" in operation.before.model_fields_set
                    and operation.before.subtasks is not None
                    else []
                )
                before_subtask_ids = {subtask.id for subtask in before_subtasks}
                if any(subtask.sourceTask is not None for subtask in before_subtasks):
                    raise PlanSemanticError(
                        f"update operation {operation.operationId!r} may use sourceTask only in "
                        "after.subtasks"
                    )
            for subtask in after_subtasks:
                source = subtask.sourceTask
                if source is None:
                    continue
                if subtask.id in before_subtask_ids:
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} uses sourceTask on existing "
                        f"subtask {subtask.id!r}; provenance is only valid for a new subtask"
                    )
                if source.id == operation.target.id:
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} cannot convert its own target "
                        "into a subtask"
                    )
                previous_conversion = converted_sources.get(source.id)
                if previous_conversion is not None:
                    raise PlanSemanticError(
                        f"source task {source.id!r} is converted more than once by operations "
                        f"{previous_conversion!r} and {operation.operationId!r}"
                    )
                converted_sources[source.id] = operation.operationId
                source_trash = trash_by_target.get(source.id)
                if source_trash is None:
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} converts source task {source.id!r}; "
                        "add a corresponding later trash operation"
                    )
                if source_trash.target.type != "task" or source_trash.target.title != source.title:
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} sourceTask title/type does not match "
                        f"trash operation {source_trash.operationId!r}"
                    )
                if (
                    operation_positions[source_trash.operationId]
                    <= operation_positions[operation.operationId]
                ):
                    raise PlanSemanticError(
                        f"source task {source.id!r} must be trashed after its replacement subtask "
                        "is created"
                    )
                if operation.operationId not in source_trash.dependsOnOperations:
                    raise PlanSemanticError(
                        f"trash operation {source_trash.operationId!r} must depend on "
                        f"{operation.operationId!r} before converting source task {source.id!r}"
                    )

        if operation.target.type == "recurringTask":
            if isinstance(operation, CompleteOperation):
                raise PlanSemanticError(
                    f"complete operation {operation.operationId!r} targets a recurrence series; "
                    "complete an explicit generated occurrence instead"
                )
            if isinstance(operation, (UpdateOperation, CreateOperation)):
                fields = operation.after.model_fields_set
                if isinstance(operation, UpdateOperation):
                    fields |= operation.before.model_fields_set
                unsupported = sorted(fields - recurring_task_fields)
                if unsupported:
                    raise PlanSemanticError(
                        f"operation {operation.operationId!r} uses non-series recurringTask "
                        "field(s): " + ", ".join(unsupported)
                    )
                if isinstance(operation, CreateOperation) and "cadence" not in fields:
                    raise PlanSemanticError(
                        f"create operation {operation.operationId!r} for recurringTask requires "
                        "an explicit cadence"
                    )
                if isinstance(operation, CreateOperation):
                    try:
                        CreateRecurringTaskFields.model_validate(
                            operation.after.model_dump(exclude_unset=True, mode="json")
                        )
                    except ValidationError as exc:
                        location = ".".join(str(part) for part in exc.errors()[0]["loc"])
                        raise PlanSemanticError(
                            f"create operation {operation.operationId!r} has invalid "
                            f"recurringTask fields at {location}"
                        ) from exc
        elif isinstance(operation, (UpdateOperation, CreateOperation)):
            fields = operation.after.model_fields_set
            if isinstance(operation, UpdateOperation):
                fields |= operation.before.model_fields_set
            unsupported = sorted(fields - task_fields)
            if unsupported:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} uses recurrence-series field(s) on "
                    f"a {operation.target.type}: " + ", ".join(unsupported)
                )
            if isinstance(operation, CreateOperation):
                try:
                    CreateTaskFields.model_validate(
                        operation.after.model_dump(exclude_unset=True, mode="json")
                    )
                except ValidationError as exc:
                    location = ".".join(str(part) for part in exc.errors()[0]["loc"])
                    raise PlanSemanticError(
                        f"create operation {operation.operationId!r} has invalid "
                        f"{operation.target.type} fields at {location}"
                    ) from exc

        if operation.target.type == "project" and isinstance(
            operation, (UpdateOperation, CreateOperation)
        ):
            fields = (
                operation.after.model_fields_set
                if isinstance(operation, CreateOperation)
                else operation.after.model_fields_set | operation.before.model_fields_set
            )
            unsupported = sorted(
                fields & {"dependencies", "masterRank", "starPriority", "subtasks"}
            )
            if unsupported:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} uses task-only project field(s): "
                    + ", ".join(unsupported)
                )

        field_sets = []
        if isinstance(operation, UpdateOperation):
            field_sets.extend((operation.before, operation.after))
        elif isinstance(operation, CreateOperation):
            field_sets.append(operation.after)
        for fields in field_sets:
            dumped = fields.model_dump(exclude_unset=True, mode="json")
            parent = dumped.get("parent")
            if parent is None or parent["id"] not in all_creates:
                continue
            if parent["id"] not in prior_creates:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} references project created later in "
                    "the plan; create parents before their children"
                )
            parent_create = prior_creates[parent["id"]]
            if parent_create.target.type != "project":
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} uses newly created non-project "
                    f"{parent['id']!r} as its parent"
                )
            if parent_create.operationId not in operation.dependsOnOperations:
                raise PlanSemanticError(
                    f"operation {operation.operationId!r} references project created by "
                    f"{parent_create.operationId!r}; add it to dependsOnOperations"
                )

        if isinstance(operation, CompleteOperation):
            completed_at = datetime.fromisoformat(operation.completedAt.replace("Z", "+00:00"))
            if completed_at > plan_created_at:
                raise PlanSemanticError(
                    f"complete operation {operation.operationId!r} completedAt is later than "
                    "the plan's createdAt"
                )

        if isinstance(operation, UpdateOperation) and "title" in operation.after.model_fields_set:
            assert operation.after.title is not None
            current_target_titles[operation.target.id] = operation.after.title
        prior_target_operations[operation.target.id] = operation
        first_target_operations.setdefault(operation.target.id, operation)

        prior_operation_ids.add(operation.operationId)
        if isinstance(operation, CreateOperation):
            prior_creates[operation.target.id] = operation


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
