"""Pure, deterministic change-plan presentation for the browser visualizer."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Literal

from marvin_pilot.models.plan_v1 import (
    ChangePlanV1,
    CreateOperation,
    TaskFields,
    TrashOperation,
    UpdateOperation,
)
from marvin_pilot.plan_io import plan_digest
from marvin_pilot.visualizer_fields import presentation_for, short_value

Action = Literal["create", "update", "trash"]
ACTION_ORDER: dict[Action, int] = {"create": 0, "update": 1, "trash": 2}
UNSECTIONED = "Unsectioned changes"
CREATED_PLACEHOLDERS = "Created by this plan"
TRASHED_PLACEHOLDERS = "Moved to Trash by this plan"


@dataclass(frozen=True, slots=True)
class CardItemView:
    field: str
    label: str
    kind: str
    text: str
    exact: str
    cleared: bool


@dataclass(frozen=True, slots=True)
class TaskCardView:
    title: str
    title_source: str
    sparse_label: str
    items: tuple[CardItemView, ...]
    note_state: str
    note: str | None
    fields_shown: int


@dataclass(frozen=True, slots=True)
class FieldValueView:
    state: str
    summary: str
    exact: str


@dataclass(frozen=True, slots=True)
class FieldDiffView:
    field: str
    label: str
    before: FieldValueView
    after: FieldValueView


@dataclass(frozen=True, slots=True)
class OperationView:
    original_index: int
    operation_id: str
    action: Action
    reason: str
    target_id: str
    target_title: str
    depends_on_operations: tuple[str, ...]
    before: TaskCardView | None
    after: TaskCardView | None
    before_empty_label: str | None
    after_empty_label: str | None
    diffs: tuple[FieldDiffView, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SectionView:
    key: str
    title: str
    counts: dict[str, int]
    operation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LayoutsView:
    split: tuple[SectionView, ...]
    before: tuple[SectionView, ...]
    after: tuple[SectionView, ...]


@dataclass(frozen=True, slots=True)
class PlanView:
    schema_version: int
    supported_schema_versions: tuple[int, ...]
    plan_id: str
    created_at: str
    summary: str
    digest: str
    source_name: str | None
    counts: dict[str, int]
    total_operations: int
    operations: tuple[OperationView, ...]
    layouts: LayoutsView

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view without exposing Pydantic or mutation objects."""

        return asdict(self)


def _exact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _field_value(field: str, value: Any, *, present: bool) -> FieldValueView:
    if not present:
        return FieldValueView("absent", "Not present", "")
    if value is None:
        return FieldValueView("clear", "Cleared", "null")
    return FieldValueView("value", short_value(field, value), _exact_json(value))


def _ordered_fields(names: set[str]) -> list[str]:
    return sorted(names, key=lambda field: (presentation_for(field).order, field))


def _task_card(fields: TaskFields, fallback_title: str, sparse_label: str) -> TaskCardView:
    values = fields.model_dump(exclude_unset=True, mode="json")
    title_value = values.get("title")
    title = title_value if isinstance(title_value, str) else fallback_title
    title_source = "plan" if isinstance(title_value, str) else "target"
    items = []
    for field in _ordered_fields(set(values) - {"title", "note"}):
        value = values[field]
        spec = presentation_for(field)
        items.append(
            CardItemView(
                field=field,
                label=spec.label,
                kind=spec.kind,
                text=short_value(field, value),
                exact=_exact_json(value),
                cleared=value is None,
            )
        )
    if "note" not in values:
        note_state, note = "absent", None
    elif values["note"] is None:
        note_state, note = "clear", None
    else:
        note_state, note = "value", values["note"]
    return TaskCardView(
        title=title,
        title_source=title_source,
        sparse_label=sparse_label,
        items=tuple(items),
        note_state=note_state,
        note=note,
        fields_shown=len(values),
    )


def _target_only_card(title: str) -> TaskCardView:
    return TaskCardView(
        title=title,
        title_source="target",
        sparse_label="Review metadata only; task fields were not captured.",
        items=(),
        note_state="absent",
        note=None,
        fields_shown=0,
    )


def _field_diffs(
    before: dict[str, Any], after: dict[str, Any], names: set[str]
) -> tuple[FieldDiffView, ...]:
    result = []
    for field in _ordered_fields(names):
        spec = presentation_for(field)
        result.append(
            FieldDiffView(
                field=field,
                label=spec.label,
                before=_field_value(field, before.get(field), present=field in before),
                after=_field_value(field, after.get(field), present=field in after),
            )
        )
    return tuple(result)


def _parent_title(fields: TaskFields | None) -> str | None:
    if fields is None or "parent" not in fields.model_fields_set or fields.parent is None:
        return None
    return fields.parent.title


def _fallback_section(fields: TaskFields | None) -> str:
    if fields is None:
        return UNSECTIONED
    parent_title = _parent_title(fields)
    if parent_title:
        return parent_title
    if "dailySection" in fields.model_fields_set and fields.dailySection:
        return fields.dailySection
    return UNSECTIONED


def _side_section(
    operation: UpdateOperation | CreateOperation | TrashOperation,
    side: Literal["before", "after"],
) -> str:
    display_value = None
    if operation.display is not None:
        display_value = getattr(operation.display, f"{side}Section")
    if display_value:
        return display_value
    fields = getattr(operation, side, None)
    return _fallback_section(fields)


def _display_warnings(
    operation: UpdateOperation | CreateOperation | TrashOperation,
) -> tuple[str, ...]:
    if operation.display is None:
        return ()
    warnings = []
    for side in ("before", "after"):
        display_title = getattr(operation.display, f"{side}Section")
        fields = getattr(operation, side, None)
        parent_title = _parent_title(fields)
        if display_title and parent_title and display_title != parent_title:
            warnings.append(
                f"{side.title()} display section {display_title!r} differs from "
                f"parent title {parent_title!r}."
            )
    return tuple(warnings)


def _operation_view(
    operation: UpdateOperation | CreateOperation | TrashOperation, index: int
) -> OperationView:
    common = {
        "original_index": index,
        "operation_id": operation.operationId,
        "action": operation.action,
        "reason": operation.reason,
        "target_id": operation.target.id,
        "target_title": getattr(operation.target, "title", "New task"),
        "depends_on_operations": tuple(operation.dependsOnOperations),
        "warnings": _display_warnings(operation),
    }
    if isinstance(operation, UpdateOperation):
        before_values = operation.before.model_dump(exclude_unset=True, mode="json")
        after_values = operation.after.model_dump(exclude_unset=True, mode="json")
        return OperationView(
            **common,
            before=_task_card(
                operation.before,
                operation.target.title,
                "Changed fields shown; unchanged fields omitted.",
            ),
            after=_task_card(
                operation.after,
                operation.target.title,
                "Changed fields shown; unchanged fields omitted.",
            ),
            before_empty_label=None,
            after_empty_label=None,
            diffs=_field_diffs(before_values, after_values, set(after_values)),
        )
    if isinstance(operation, CreateOperation):
        after_values = operation.after.model_dump(exclude_unset=True, mode="json")
        return OperationView(
            **common,
            before=None,
            after=_task_card(operation.after, operation.after.title, "Proposed task fields."),
            before_empty_label="No before state — created by this plan",
            after_empty_label=None,
            diffs=_field_diffs({}, after_values, set(after_values)),
        )
    lifecycle_before = FieldValueView("value", "Active", '"active"')
    lifecycle_after = FieldValueView("value", "Moved to Marvin Trash", '"trash"')
    return OperationView(
        **common,
        before=_target_only_card(operation.target.title),
        after=None,
        before_empty_label=None,
        after_empty_label="No active after state — moved to Marvin Trash",
        diffs=(FieldDiffView("lifecycle", "Task state", lifecycle_before, lifecycle_after),),
    )


def _section_entries(
    plan: ChangePlanV1,
    operations: tuple[OperationView, ...],
    layout: Literal["split", "before", "after"],
) -> list[tuple[str, OperationView]]:
    entries = []
    for source, view in zip(plan.operations, operations, strict=True):
        before_section = _side_section(source, "before")
        after_section = _side_section(source, "after")
        if layout == "before":
            title = CREATED_PLACEHOLDERS if view.action == "create" else before_section
        elif layout == "after":
            title = TRASHED_PLACEHOLDERS if view.action == "trash" else after_section
        elif view.action == "update" and before_section != after_section:
            title = f"{before_section} → {after_section}"
        elif view.action == "create":
            title = after_section
        else:
            title = before_section
        entries.append((title, view))
    return entries


def _sections(
    entries: list[tuple[str, OperationView]], layout: str
) -> tuple[SectionView, ...]:
    grouped: OrderedDict[str, list[OperationView]] = OrderedDict()
    for title, operation in entries:
        grouped.setdefault(title, []).append(operation)
    result = []
    for position, (title, members) in enumerate(grouped.items()):
        ordered = sorted(
            members,
            key=lambda item: (ACTION_ORDER[item.action], item.original_index),
        )
        counts = {
            action: sum(member.action == action for member in ordered)
            for action in ("create", "update", "trash")
        }
        result.append(
            SectionView(
                key=f"{layout}:{position}",
                title=title,
                counts=counts,
                operation_ids=tuple(member.operation_id for member in ordered),
            )
        )
    return tuple(result)


def build_plan_view(plan: ChangePlanV1, *, source_name: str | None = None) -> PlanView:
    """Build all immutable visualizer projections from one validated plan."""

    operations = tuple(
        _operation_view(operation, index)
        for index, operation in enumerate(plan.operations, start=1)
    )
    counts = {
        action: sum(operation.action == action for operation in operations)
        for action in ("create", "update", "trash")
    }
    layouts = LayoutsView(
        split=_sections(_section_entries(plan, operations, "split"), "split"),
        before=_sections(_section_entries(plan, operations, "before"), "before"),
        after=_sections(_section_entries(plan, operations, "after"), "after"),
    )
    return PlanView(
        schema_version=plan.schemaVersion,
        supported_schema_versions=(1,),
        plan_id=plan.planId,
        created_at=plan.createdAt,
        summary=plan.summary,
        digest=plan_digest(plan),
        source_name=source_name,
        counts=counts,
        total_operations=len(operations),
        operations=operations,
        layouts=layouts,
    )
