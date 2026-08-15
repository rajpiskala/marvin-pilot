"""Pure, deterministic change-plan presentation for the browser visualizer."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from marvin_pilot.models.plan_v1 import (
    ChangePlanV1,
    CompleteOperation,
    CreateOperation,
    DaySectionRef,
    HierarchyPathNode,
    TaskFields,
    TrashOperation,
    UpdateOperation,
)
from marvin_pilot.plan_io import SUPPORTED_SCHEMA_VERSIONS, plan_digest
from marvin_pilot.visualizer_fields import presentation_for, short_value

Action = Literal["create", "update", "complete", "trash"]
NodeType = Literal["inbox", "category", "project", "task", "unknown"]
PathState = Literal["path", "root", "legacy", "unknown"]
ACTION_ORDER: dict[Action, int] = {"create": 0, "update": 1, "complete": 2, "trash": 3}
UNSECTIONED = "Unsectioned changes"
CREATED_PLACEHOLDERS = "Created by this plan"
TRASHED_PLACEHOLDERS = "Moved to Trash by this plan"
LEADING_TIME = re.compile(
    r"^\s*(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?(?=\s)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CardItemView:
    field: str
    label: str
    kind: str
    text: str
    exact: str
    cleared: bool


@dataclass(frozen=True, slots=True)
class SubtaskItemView:
    id: str
    title: str
    done: bool
    order: int
    source_task_id: str | None
    source_task_title: str | None


@dataclass(frozen=True, slots=True)
class TaskCardView:
    title: str
    title_source: str
    sparse_label: str
    items: tuple[CardItemView, ...]
    note_state: str
    note: str | None
    subtasks: tuple[SubtaskItemView, ...]
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
class PathNodeView:
    id: str
    type: NodeType
    title: str
    emoji: str | None
    color: str | None
    order: int | None


@dataclass(frozen=True, slots=True)
class DaySectionPlacementView:
    state: Literal["value", "none", "unknown"]
    key: str | None
    title: str | None
    order: int | None


@dataclass(frozen=True, slots=True)
class OperationView:
    original_index: int
    operation_id: str
    action: Action
    target_type: Literal["task", "project"]
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
    change_kinds: tuple[str, ...]
    before_path_state: PathState
    after_path_state: PathState
    before_path: tuple[PathNodeView, ...]
    after_path: tuple[PathNodeView, ...]
    before_order: int
    after_order: int
    before_day_section: DaySectionPlacementView
    after_day_section: DaySectionPlacementView


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
class PreviewNodeView:
    key: str
    id: str
    type: NodeType
    title: str
    emoji: str | None
    color: str | None
    order: int | None
    context_only: bool
    operation_id: str | None
    action: Action | None
    card: TaskCardView | None
    children: tuple[PreviewNodeView, ...]


@dataclass(slots=True)
class _MutablePreviewNode:
    id: str
    type: NodeType
    title: str
    emoji: str | None
    color: str | None
    order: int | None
    context_only: bool = True
    operation_id: str | None = None
    action: Action | None = None
    card: TaskCardView | None = None
    children: OrderedDict[str, _MutablePreviewNode] | None = None

    def child_map(self) -> OrderedDict[str, _MutablePreviewNode]:
        if self.children is None:
            self.children = OrderedDict()
        return self.children


@dataclass(frozen=True, slots=True)
class DaySectionGroupView:
    key: str
    title: str
    order: int
    context_state: Literal["value", "none", "unknown"]
    operation_ids: tuple[str, ...]
    roots: tuple[PreviewNodeView, ...]


@dataclass(frozen=True, slots=True)
class StatePreviewView:
    roots: tuple[PreviewNodeView, ...]
    day_sections: tuple[DaySectionGroupView, ...]
    incomplete_operation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreviewsView:
    show_day_sections_by_default: bool
    before: StatePreviewView
    after: StatePreviewView


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
    previews: PreviewsView

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe view without exposing Pydantic or mutation objects."""

        return json.loads(json.dumps(asdict(self), ensure_ascii=False))


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
    for field in _ordered_fields(set(values) - {"title", "note", "subtasks"}):
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
    subtasks = tuple(
        SubtaskItemView(
            id=item["id"],
            title=item["title"],
            done=item.get("done", False),
            order=index,
            source_task_id=(item.get("sourceTask") or {}).get("id"),
            source_task_title=(item.get("sourceTask") or {}).get("title"),
        )
        for index, item in enumerate(values.get("subtasks") or [], start=1)
    )
    return TaskCardView(
        title=title,
        title_source=title_source,
        sparse_label=sparse_label,
        items=tuple(items),
        note_state=note_state,
        note=note,
        subtasks=subtasks,
        fields_shown=len(values),
    )


def _target_only_card(title: str) -> TaskCardView:
    return TaskCardView(
        title=title,
        title_source="target",
        sparse_label="Review metadata only; item fields were not captured.",
        items=(),
        note_state="absent",
        note=None,
        subtasks=(),
        fields_shown=0,
    )


def _card_with_display_parent(
    card: TaskCardView,
    path_state: PathState,
    path: tuple[PathNodeView, ...],
) -> TaskCardView:
    """Use the canonical display-path title when a parent is being changed."""

    if path_state != "path" or not path:
        return card
    parent = path[-1]
    items = tuple(
        replace(
            item,
            text=parent.title,
            exact=_exact_json({"id": parent.id, "title": parent.title}),
        )
        if item.field == "parent" and not item.cleared
        else item
        for item in card.items
    )
    return replace(card, items=items)


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
    operation: UpdateOperation | CreateOperation | CompleteOperation | TrashOperation,
    side: Literal["before", "after"],
) -> str:
    display_value = None
    if operation.display is not None:
        display_value = getattr(operation.display, f"{side}Section")
    if display_value:
        return display_value
    if operation.display is not None:
        path = getattr(operation.display, f"{side}Path")
        if path:
            return path[-1].title
    fields = getattr(operation, side, None)
    return _fallback_section(fields)


def _display_warnings(
    operation: UpdateOperation | CreateOperation | CompleteOperation | TrashOperation,
) -> tuple[str, ...]:
    if not isinstance(operation, UpdateOperation) or operation.display is None:
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


def _path_node_view(node: HierarchyPathNode) -> PathNodeView:
    return PathNodeView(
        id=node.id,
        type=node.type,
        title=node.title,
        emoji=node.emoji,
        color=node.color,
        order=node.order,
    )


def _legacy_path(
    operation: UpdateOperation | CreateOperation | CompleteOperation | TrashOperation,
    side: Literal["before", "after"],
) -> tuple[PathState, tuple[PathNodeView, ...]]:
    display_title = None
    if operation.display is not None:
        display_title = getattr(operation.display, f"{side}Section")
    fields = getattr(operation, side, None)
    parent = None
    if fields is not None and "parent" in fields.model_fields_set:
        parent = fields.parent
    if display_title:
        if parent is not None and parent.title == display_title:
            node_id = parent.id
        else:
            node_id = f"legacy-section:{display_title}"
        return "legacy", (PathNodeView(node_id, "unknown", display_title, None, None, None),)
    if parent is not None:
        return "legacy", (
            PathNodeView(
                parent.id,
                "unknown",
                parent.title or "Unknown parent",
                None,
                None,
                None,
            ),
        )
    return "unknown", ()


def _side_path(
    operation: UpdateOperation | CreateOperation | CompleteOperation | TrashOperation,
    side: Literal["before", "after"],
) -> tuple[PathState, tuple[PathNodeView, ...]]:
    if operation.display is not None:
        field_name = f"{side}Path"
        if field_name in operation.display.model_fields_set:
            path = getattr(operation.display, field_name)
            if path is None:
                return "unknown", ()
            if not path:
                return "root", ()
            return "path", tuple(_path_node_view(node) for node in path)
    return _legacy_path(operation, side)


def _display_order(
    operation: UpdateOperation | CreateOperation | CompleteOperation | TrashOperation,
    side: Literal["before", "after"],
    fallback: int,
) -> int:
    if operation.display is None:
        return fallback
    value = getattr(operation.display, f"{side}Order")
    return fallback if value is None else value


def _change_kinds(
    operation: UpdateOperation | CreateOperation | CompleteOperation | TrashOperation,
    before_state: PathState,
    before_path: tuple[PathNodeView, ...],
    after_state: PathState,
    after_path: tuple[PathNodeView, ...],
) -> tuple[str, ...]:
    if isinstance(operation, CreateOperation):
        return ("created",)
    if isinstance(operation, CompleteOperation):
        return ("completed",)
    if isinstance(operation, TrashOperation):
        return ("trashed",)

    before = operation.before.model_dump(exclude_unset=True, mode="json")
    after = operation.after.model_dump(exclude_unset=True, mode="json")
    kinds: list[str] = []
    if "title" in after and before.get("title") != after.get("title"):
        kinds.append("renamed")
    path_known = before_state in {"path", "root"} and after_state in {"path", "root"}
    path_moved = path_known and tuple(node.id for node in before_path) != tuple(
        node.id for node in after_path
    )
    if before.get("parent") != after.get("parent") or path_moved:
        kinds.append("moved")
    schedule_fields = {"scheduledDate", "scheduledTime", "plannedWeek", "plannedMonth"}
    if any(field in after and before.get(field) != after.get(field) for field in schedule_fields):
        kinds.append("rescheduled")
    handled = {"title", "parent", *schedule_fields}
    if any(field not in handled and before.get(field) != value for field, value in after.items()):
        kinds.append("edited")
    return tuple(kinds or ["updated"])


def _day_section_placement(
    operation: UpdateOperation | CreateOperation | CompleteOperation | TrashOperation,
    side: Literal["before", "after"],
) -> DaySectionPlacementView:
    if operation.display is None:
        return DaySectionPlacementView("unknown", None, None, None)
    field_name = f"{side}DaySection"
    if field_name not in operation.display.model_fields_set:
        return DaySectionPlacementView("unknown", None, None, None)
    section: DaySectionRef | None = getattr(operation.display, field_name)
    if section is None:
        return DaySectionPlacementView("none", None, None, None)
    return DaySectionPlacementView("value", section.key, section.title, section.order)


def _operation_view(
    operation: UpdateOperation | CreateOperation | CompleteOperation | TrashOperation, index: int
) -> OperationView:
    before_path_state, before_path = _side_path(operation, "before")
    after_path_state, after_path = _side_path(operation, "after")
    common = {
        "original_index": index,
        "operation_id": operation.operationId,
        "action": operation.action,
        "target_type": operation.target.type,
        "reason": operation.reason,
        "target_id": operation.target.id,
        "target_title": getattr(operation.target, "title", f"New {operation.target.type}"),
        "depends_on_operations": tuple(operation.dependsOnOperations),
        "warnings": _display_warnings(operation),
        "change_kinds": _change_kinds(
            operation,
            before_path_state,
            before_path,
            after_path_state,
            after_path,
        ),
        "before_path_state": before_path_state,
        "after_path_state": after_path_state,
        "before_path": before_path,
        "after_path": after_path,
        "before_order": _display_order(operation, "before", index),
        "after_order": _display_order(operation, "after", index),
        "before_day_section": _day_section_placement(operation, "before"),
        "after_day_section": _day_section_placement(operation, "after"),
    }
    if isinstance(operation, UpdateOperation):
        before_values = operation.before.model_dump(exclude_unset=True, mode="json")
        after_values = operation.after.model_dump(exclude_unset=True, mode="json")
        before_card = _card_with_display_parent(
            _task_card(
                operation.before,
                operation.target.title,
                "Changed fields shown; unchanged fields omitted.",
            ),
            before_path_state,
            before_path,
        )
        after_card = _card_with_display_parent(
            _task_card(
                operation.after,
                operation.target.title,
                "Changed fields shown; unchanged fields omitted.",
            ),
            after_path_state,
            after_path,
        )
        return OperationView(
            **common,
            before=before_card,
            after=after_card,
            before_empty_label=None,
            after_empty_label=None,
            diffs=_field_diffs(before_values, after_values, set(after_values)),
        )
    if isinstance(operation, CreateOperation):
        after_values = operation.after.model_dump(exclude_unset=True, mode="json")
        after_card = _card_with_display_parent(
            _task_card(
                operation.after,
                operation.after.title,
                f"Proposed {operation.target.type} fields.",
            ),
            after_path_state,
            after_path,
        )
        return OperationView(
            **common,
            before=None,
            after=after_card,
            before_empty_label="No before state — created by this plan",
            after_empty_label=None,
            diffs=_field_diffs({}, after_values, set(after_values)),
        )
    if isinstance(operation, CompleteOperation):
        lifecycle_before = FieldValueView("value", "Active", '"active"')
        lifecycle_after = FieldValueView(
            "value", f"Completed at {operation.completedAt}", _exact_json(operation.completedAt)
        )
        return OperationView(
            **common,
            before=_target_only_card(operation.target.title),
            after=_target_only_card(operation.target.title),
            before_empty_label=None,
            after_empty_label=None,
            diffs=(
                FieldDiffView(
                    "lifecycle",
                    f"{operation.target.type.title()} state",
                    lifecycle_before,
                    lifecycle_after,
                ),
            ),
        )
    lifecycle_before = FieldValueView("value", "Active", '"active"')
    lifecycle_after = FieldValueView("value", "Moved to Marvin Trash", '"trash"')
    return OperationView(
        **common,
        before=_target_only_card(operation.target.title),
        after=None,
        before_empty_label=None,
        after_empty_label="No active after state — moved to Marvin Trash",
        diffs=(
            FieldDiffView(
                "lifecycle",
                f"{operation.target.type.title()} state",
                lifecycle_before,
                lifecycle_after,
            ),
        ),
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


def _leading_time_minutes(title: str) -> int | None:
    """Return a leading Marvin-style title time as minutes after midnight."""

    match = LEADING_TIME.match(title)
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = match.group("meridiem")
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    elif hour > 23:
        return None
    return hour * 60 + minute


def _operation_time(operation: OperationView, layout: str) -> int | None:
    if layout == "before":
        cards = (operation.before, operation.after)
    else:
        # Split follows the proposed schedule so paired before/after rows never drift.
        cards = (operation.after, operation.before)
    for card in cards:
        if card is not None and (minutes := _leading_time_minutes(card.title)) is not None:
            return minutes
    return None


def _operation_sort_key(operation: OperationView, layout: str) -> tuple[int, int, int]:
    minutes = _operation_time(operation, layout)
    if minutes is not None:
        return (0, minutes, operation.original_index)
    return (1, ACTION_ORDER[operation.action], operation.original_index)


def _sections(entries: list[tuple[str, OperationView]], layout: str) -> tuple[SectionView, ...]:
    grouped: OrderedDict[str, list[OperationView]] = OrderedDict()
    for title, operation in entries:
        grouped.setdefault(title, []).append(operation)
    result = []
    for position, (title, members) in enumerate(grouped.items()):
        ordered = sorted(
            members,
            key=lambda item: _operation_sort_key(item, layout),
        )
        counts = {
            action: sum(member.action == action for member in ordered)
            for action in ("create", "update", "complete", "trash")
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


def _preview_node_key(node_type: NodeType, node_id: str) -> str:
    return f"{node_type}:{node_id}"


def _ensure_preview_node(
    siblings: OrderedDict[str, _MutablePreviewNode], source: PathNodeView
) -> _MutablePreviewNode:
    key = _preview_node_key(source.type, source.id)
    existing = siblings.get(key)
    if existing is None:
        existing = _MutablePreviewNode(
            id=source.id,
            type=source.type,
            title=source.title,
            emoji=source.emoji,
            color=source.color,
            order=source.order,
        )
        siblings[key] = existing
    else:
        if existing.emoji is None and source.emoji is not None:
            existing.emoji = source.emoji
        if existing.color is None and source.color is not None:
            existing.color = source.color
        if existing.order is None and source.order is not None:
            existing.order = source.order
    return existing


def _freeze_preview_node(node: _MutablePreviewNode) -> PreviewNodeView:
    children = tuple(node.children.values()) if node.children is not None else ()
    children = tuple(
        sorted(
            enumerate(children),
            key=lambda pair: (
                pair[1].order is None,
                pair[1].order if pair[1].order is not None else 0,
                pair[0],
            ),
        )
    )
    return PreviewNodeView(
        key=_preview_node_key(node.type, node.id),
        id=node.id,
        type=node.type,
        title=node.title,
        emoji=node.emoji,
        color=node.color,
        order=node.order,
        context_only=node.context_only,
        operation_id=node.operation_id,
        action=node.action,
        card=node.card,
        children=tuple(_freeze_preview_node(child) for _, child in children),
    )


def _preview_tree(
    operations: tuple[OperationView, ...], side: Literal["before", "after"]
) -> tuple[PreviewNodeView, ...]:
    roots: OrderedDict[str, _MutablePreviewNode] = OrderedDict()
    for operation in operations:
        card = getattr(operation, side)
        if card is None:
            continue
        siblings = roots
        path: tuple[PathNodeView, ...] = getattr(operation, f"{side}_path")
        path_state: PathState = getattr(operation, f"{side}_path_state")
        if path_state == "unknown":
            path = (
                PathNodeView(
                    id="marvin-pilot:location-not-supplied",
                    type="unknown",
                    title="Location not supplied",
                    emoji=None,
                    color=None,
                    order=1_000_000,
                ),
            )
        for path_node in path:
            parent = _ensure_preview_node(siblings, path_node)
            siblings = parent.child_map()

        target_source = PathNodeView(
            id=operation.target_id,
            type=operation.target_type,
            title=card.title,
            emoji=None,
            color=None,
            order=getattr(operation, f"{side}_order"),
        )
        target = _ensure_preview_node(siblings, target_source)
        target.title = card.title
        target.context_only = False
        target.operation_id = operation.operation_id
        target.action = operation.action
        target.card = card
    return tuple(_freeze_preview_node(node) for node in roots.values())


def _day_section_groups(
    operations: tuple[OperationView, ...], side: Literal["before", "after"]
) -> tuple[DaySectionGroupView, ...]:
    grouped: OrderedDict[
        tuple[str, str | None], tuple[DaySectionPlacementView, list[OperationView]]
    ] = OrderedDict()
    for operation in operations:
        if getattr(operation, side) is None:
            continue
        placement: DaySectionPlacementView = getattr(operation, f"{side}_day_section")
        group_key = (placement.state, placement.key)
        if group_key not in grouped:
            grouped[group_key] = (placement, [])
        grouped[group_key][1].append(operation)

    sortable = []
    for position, ((state, key), (placement, members)) in enumerate(grouped.items()):
        if state == "value":
            title = placement.title or "Unnamed day section"
            order = placement.order if placement.order is not None else 0
            rendered_key = key or "unnamed"
        elif state == "none":
            title = "No day section"
            order = 1_000_001
            rendered_key = "none"
        else:
            title = "Day section not supplied"
            order = 1_000_002
            rendered_key = "unknown"
        sortable.append(
            (
                order,
                position,
                DaySectionGroupView(
                    key=rendered_key,
                    title=title,
                    order=order,
                    context_state=state,
                    operation_ids=tuple(member.operation_id for member in members),
                    roots=_preview_tree(tuple(members), side),
                ),
            )
        )
    return tuple(item[2] for item in sorted(sortable, key=lambda item: (item[0], item[1])))


def _previews(plan: ChangePlanV1, operations: tuple[OperationView, ...]) -> PreviewsView:
    show_day_sections = (
        plan.reviewDisplay.showDaySectionsByDefault if plan.reviewDisplay is not None else False
    )
    return PreviewsView(
        show_day_sections_by_default=show_day_sections,
        before=StatePreviewView(
            roots=_preview_tree(operations, "before"),
            day_sections=_day_section_groups(operations, "before"),
            incomplete_operation_ids=tuple(
                operation.operation_id
                for operation in operations
                if operation.before is not None and operation.before_path_state == "unknown"
            ),
        ),
        after=StatePreviewView(
            roots=_preview_tree(operations, "after"),
            day_sections=_day_section_groups(operations, "after"),
            incomplete_operation_ids=tuple(
                operation.operation_id
                for operation in operations
                if operation.after is not None and operation.after_path_state == "unknown"
            ),
        ),
    )


def build_plan_view(plan: ChangePlanV1, *, source_name: str | None = None) -> PlanView:
    """Build all immutable visualizer projections from one validated plan."""

    operations = tuple(
        _operation_view(operation, index)
        for index, operation in enumerate(plan.operations, start=1)
    )
    counts = {
        action: sum(operation.action == action for operation in operations)
        for action in ("create", "update", "complete", "trash")
    }
    layouts = LayoutsView(
        split=_sections(_section_entries(plan, operations, "split"), "split"),
        before=_sections(_section_entries(plan, operations, "before"), "before"),
        after=_sections(_section_entries(plan, operations, "after"), "after"),
    )
    return PlanView(
        schema_version=plan.schemaVersion,
        supported_schema_versions=SUPPORTED_SCHEMA_VERSIONS,
        plan_id=plan.planId,
        created_at=plan.createdAt,
        summary=plan.summary,
        digest=plan_digest(plan),
        source_name=source_name,
        counts=counts,
        total_operations=len(operations),
        operations=operations,
        layouts=layouts,
        previews=_previews(plan, operations),
    )
