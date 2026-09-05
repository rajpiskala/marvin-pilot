"""Compile compact, strict authoring drafts into exact reviewable change plans."""

from __future__ import annotations

import json
import math
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import Field, StrictStr, ValidationError, field_validator, model_validator

from marvin_pilot.backup_cache import load_cached_backup_documents
from marvin_pilot.backup_context import normalized_title
from marvin_pilot.compiler import compile_operation, project_compiled_mutation
from marvin_pilot.completion import (
    completion_day_behavior,
    completion_local_date,
    usable_marvin_day,
)
from marvin_pilot.duration import milliseconds_to_duration
from marvin_pilot.errors import LivePreconditionError, PlanSemanticError, PlanSyntaxError
from marvin_pilot.field_registry import live_field_matches, recurring_task_field_matches
from marvin_pilot.models.plan_v1 import (
    ChangePlanV1,
    ClosedModel,
    CompleteOperation,
    CompletionDayTransition,
    CreateItemFields,
    CreateOperation,
    ExistingItemTarget,
    ExpectedAccount,
    HierarchyPathNode,
    ItemFields,
    NewItemTarget,
    Operation,
    OperationDisplay,
    RecurringOccurrenceRef,
    RecurringTaskFields,
    SiblingOrder,
    TaskFields,
    TrashOperation,
    UpdateOperation,
)
from marvin_pilot.plan_io import canonical_plan_bytes, parse_plan_bytes
from marvin_pilot.preflight import verify_expected_account


class PrepareReader(Protocol):
    def get_doc(self, item_id: str) -> dict[str, Any] | None: ...

    def get_labels(self) -> list[dict[str, Any]]: ...

    def check_connection(self) -> Any: ...


class DraftTarget(ClosedModel):
    type: Literal["task", "project", "category", "recurringTask"]
    id: StrictStr | None = None
    title: StrictStr | None = None
    recurrence: RecurringOccurrenceRef | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> DraftTarget:
        if self.id is not None and not self.id.strip():
            raise ValueError("target.id must not be empty")
        if self.title is not None and not self.title.strip():
            raise ValueError("target.title must not be empty")
        return self


class DraftBaseOperation(ClosedModel):
    operationId: StrictStr
    action: StrictStr
    target: DraftTarget
    reason: StrictStr
    dependsOnOperations: list[StrictStr] = Field(default_factory=list)


class DraftUpdateOperation(DraftBaseOperation):
    action: Literal["update"]
    target: DraftTarget
    after: ItemFields
    siblingOrder: SiblingOrder | None = None


class DraftCreateOperation(DraftBaseOperation):
    action: Literal["create"]
    target: DraftTarget
    after: CreateItemFields

    @model_validator(mode="after")
    def validate_create_identity(self) -> DraftCreateOperation:
        if self.target.title is not None:
            raise ValueError("create target.title is not accepted; after.title is authoritative")
        return self


class DraftTrashOperation(DraftBaseOperation):
    action: Literal["trash"]


class DraftCompleteOperation(DraftBaseOperation):
    action: Literal["complete"]
    completedAt: StrictStr


DraftOperation = Annotated[
    DraftUpdateOperation | DraftCreateOperation | DraftTrashOperation | DraftCompleteOperation,
    Field(discriminator="action"),
]


class ChangeDraftV1(ClosedModel):
    draftVersion: Literal[1]
    expectedAccount: ExpectedAccount
    summary: StrictStr
    operations: Annotated[list[DraftOperation], Field(min_length=1, max_length=500)]

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be empty")
        return value


class SnapshotReader:
    """Read Marvin documents from a backup, with an optional live authoritative overlay."""

    def __init__(
        self,
        documents: list[dict[str, Any]],
        *,
        live: PrepareReader | None = None,
    ) -> None:
        self.documents = {
            document["_id"]: deepcopy(document)
            for document in documents
            if isinstance(document.get("_id"), str)
        }
        self.live = live
        self._live_cache: dict[str, dict[str, Any] | None] = {}

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        if self.live is not None:
            if item_id not in self._live_cache:
                self._live_cache[item_id] = self.live.get_doc(item_id)
            live = self._live_cache[item_id]
            return deepcopy(live) if live is not None else None
        document = self.documents.get(item_id)
        return deepcopy(document) if document is not None else None

    def get_context_doc(self, item_id: str) -> dict[str, Any] | None:
        if item_id in self._live_cache and self._live_cache[item_id] is not None:
            return deepcopy(self._live_cache[item_id])
        document = self.documents.get(item_id)
        if document is not None:
            return deepcopy(document)
        return self.get_doc(item_id)

    def put_projected(self, item_id: str, document: dict[str, Any] | None) -> None:
        self._live_cache[item_id] = deepcopy(document)

    def resolve_unique(self, target_type: str, title: str) -> str:
        """Resolve one backup title conservatively; never choose among duplicates."""

        candidates = dict(self.documents)
        candidates.update(self._live_cache)
        normalized = normalized_title(title)
        matches = [
            document
            for document in candidates.values()
            if document is not None
            and _document_type(document) == target_type
            and document.get("deletedAt") in {None, ""}
            and isinstance(document.get("title"), str)
            and normalized_title(document["title"]) == normalized
        ]
        exact = [document for document in matches if document.get("title") == title]
        matches = exact or matches
        if not matches:
            raise PlanSemanticError(
                f"no backup {target_type} has the normalized title {title!r}; supply target.id"
            )
        if len(matches) > 1:
            choices = ", ".join(
                f"{document.get('title')} [{document.get('_id')}]"
                for document in sorted(matches, key=lambda item: str(item.get("_id")))
            )
            raise PlanSemanticError(
                f"title {title!r} is ambiguous for {target_type}; supply target.id: {choices}"
            )
        return str(matches[0]["_id"])

    def display_order(self, document: dict[str, Any]) -> int | None:
        """Return a deterministic sibling ordinal from available backup/projected state."""

        item_type = _document_type(document)
        if item_type is None:
            return None
        candidates = dict(self.documents)
        candidates.update(self._live_cache)
        if isinstance(document.get("_id"), str):
            candidates[document["_id"]] = document
        parent_id = document.get("parentId")
        day = document.get("day")
        if item_type == "task" and day not in {None, "", "unassigned"}:
            rank_field = "rank"
            siblings = [
                item
                for item in candidates.values()
                if item is not None
                and _document_type(item) == "task"
                and item.get("day") == day
                and item.get("deletedAt") in {None, ""}
            ]
        else:
            rank_field = "rank" if item_type in {"project", "category"} else "masterRank"
            siblings = [
                item
                for item in candidates.values()
                if item is not None
                and _document_type(item) == item_type
                and item.get("parentId") == parent_id
                and item.get("deletedAt") in {None, ""}
            ]

        def key(item: dict[str, Any]) -> tuple[float, str, str]:
            rank = item.get(rank_field)
            order = (
                float(rank)
                if isinstance(rank, (int, float))
                and not isinstance(rank, bool)
                and math.isfinite(rank)
                else math.inf
            )
            return (order, str(item.get("title", "")).casefold(), str(item.get("_id", "")))

        target_id = document.get("_id")
        for index, item in enumerate(sorted(siblings, key=key)):
            if item.get("_id") == target_id:
                return index
        return None


def parse_draft_bytes(raw: bytes) -> ChangeDraftV1:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanSyntaxError(f"draft is not strict UTF-8 JSON: {exc}") from exc
    try:
        return ChangeDraftV1.model_validate(value)
    except ValidationError as exc:
        raise PlanSyntaxError(f"invalid Marvin Pilot draft: {exc}") from exc


def _document_type(document: dict[str, Any]) -> str | None:
    if document.get("db") == "Tasks":
        return "task"
    if document.get("db") == "Categories" and document.get("type") in {"category", "project"}:
        return str(document["type"])
    if document.get("db") == "RecurringTasks" and document.get("recurringType") == "task":
        return "recurringTask"
    return None


def _path_for_parent(parent_id: str | None, reader: SnapshotReader) -> list[HierarchyPathNode]:
    if parent_id in {None, "", "unassigned", "root"}:
        return []
    reverse: list[HierarchyPathNode] = []
    seen: set[str] = set()
    current = parent_id
    while current not in {None, "", "unassigned", "root"}:
        assert current is not None
        if current in seen:
            raise PlanSemanticError(f"cannot prepare hierarchy: cycle at {current!r}")
        seen.add(current)
        document = reader.get_context_doc(current)
        if document is None:
            raise PlanSemanticError(f"cannot prepare hierarchy: parent {current!r} not found")
        item_type = _document_type(document)
        if item_type not in {"category", "project"}:
            raise PlanSemanticError(
                f"cannot prepare hierarchy: parent {current!r} is not a container"
            )
        title = document.get("title")
        if not isinstance(title, str) or not title:
            raise PlanSemanticError(f"cannot prepare hierarchy: parent {current!r} has no title")
        reverse.append(
            HierarchyPathNode(
                id=current,
                type=item_type,
                title=title,
                emoji=document.get("emoji") if isinstance(document.get("emoji"), str) else None,
                color=document.get("color") if isinstance(document.get("color"), str) else None,
            )
        )
        current = document.get("parentId")
    return list(reversed(reverse))


def _display_for(
    document: dict[str, Any] | None,
    after: ItemFields | None,
    reader: SnapshotReader,
    *,
    action: str,
) -> OperationDisplay | None:
    before_path = None
    after_path = None
    if action != "create" and document is not None:
        before_path = _path_for_parent(document.get("parentId"), reader)
    if action != "trash":
        parent_id = document.get("parentId") if document is not None else "unassigned"
        if after is not None and "parent" in after.model_fields_set:
            parent_id = after.parent.id if after.parent is not None else "unassigned"
        after_path = _path_for_parent(parent_id, reader)
    existing_completed_at = None
    if (
        action == "update"
        and document is not None
        and document.get("db") == "Tasks"
        and document.get("done") is True
    ):
        done_at = document.get("doneAt")
        if isinstance(done_at, (int, float)) and not isinstance(done_at, bool) and done_at > 0:
            with suppress(OSError, OverflowError, ValueError):
                existing_completed_at = (
                    datetime.fromtimestamp(done_at / 1000, UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
        if existing_completed_at is None:
            raise PlanSemanticError(
                "cannot prepare update for completed item without a trustworthy doneAt timestamp"
            )
    display: dict[str, Any] = {}
    if action != "create":
        display["beforePath"] = before_path
        before_order = reader.display_order(document) if document is not None else None
        if before_order is not None:
            display["beforeOrder"] = before_order
    if action != "trash":
        display["afterPath"] = after_path
        if document is not None:
            projected = deepcopy(document)
            if after is not None:
                if "parent" in after.model_fields_set:
                    projected["parentId"] = (
                        after.parent.id if after.parent is not None else "unassigned"
                    )
                if "scheduledDate" in after.model_fields_set:
                    projected["day"] = after.scheduledDate or "unassigned"
                if "title" in after.model_fields_set:
                    projected["title"] = after.title
            after_order = reader.display_order(projected)
            if after_order is not None:
                display["afterOrder"] = after_order
    if existing_completed_at is not None:
        display["existingCompletedAt"] = existing_completed_at
    return OperationDisplay.model_validate(display)


def _before_fields(target_type: str, after: ItemFields, document: dict[str, Any]) -> ItemFields:
    values: dict[str, Any] = {}
    for field in after.model_fields_set:
        if target_type == "recurringTask":
            # Convert through the public normalizer by testing candidate plan values. The
            # native-to-plan shapes that need richer reconstruction are rejected explicitly.
            native_name = {
                "title": "title",
                "parent": "parentId",
                "labels": "labelIds",
                "estimatedTimeDuration": "timeEstimate",
                "note": "note",
                "subtasks": "subtaskList",
                "starPriority": "isStarred",
                "frogSize": "isFrogged",
                "dueInDays": "dueIn",
            }.get(field)
            if native_name is None:
                raise PlanSemanticError(
                    f"prepare cannot mechanically reconstruct recurring field {field!r}; "
                    "author a full plan for this field"
                )
        else:
            native_name = {
                "title": "title",
                "parent": "parentId",
                "scheduledDate": "day",
                "dueDate": "dueDate",
                "startDate": "startDate",
                "endDate": "endDate",
                "plannedWeek": "plannedWeek",
                "plannedMonth": "plannedMonth",
                "labels": "labelIds",
                "estimatedTimeDuration": "timeEstimate",
                "note": "note",
                "subtasks": "subtasks",
                "dayRank": "rank",
                "masterRank": "masterRank",
                "dailySection": "dailySection",
                "bonusSection": "bonusSection",
                "customSectionId": "customSection",
                "timeBlockSectionId": "timeBlockSection",
                "starPriority": "isStarred",
                "frogSize": "isFrogged",
                "backburner": "backburner",
                "reviewDate": "reviewDate",
                "snoozedUntil": "itemSnoozeTime",
                "permanentSnoozeUntil": "permaSnoozeTime",
                "dependencies": "dependsOn",
                "orbit": "orbit",
            }[field]
        raw = document.get(native_name)
        if field == "parent":
            values[field] = None if raw in {None, "", "unassigned", "root"} else {"id": raw}
        elif field == "labels":
            values[field] = [{"id": identifier} for identifier in (raw or [])]
        elif field == "scheduledDate":
            values[field] = None if raw in {None, "", "unassigned"} else raw
        elif field == "estimatedTimeDuration":
            if raw in {None, 0}:
                values[field] = None
            else:
                try:
                    values[field] = milliseconds_to_duration(raw)
                except ValueError as exc:
                    raise PlanSemanticError(
                        "cannot prepare a non-whole-minute time estimate"
                    ) from exc
        elif field == "subtasks":
            source = list((raw or {}).values()) if isinstance(raw, dict) else (raw or [])
            source = sorted(source, key=lambda item: item.get("rank", 0))
            values[field] = []
            for item in source:
                prepared_subtask = {"id": item.get("_id"), "title": item.get("title")}
                if target_type != "recurringTask":
                    prepared_subtask["done"] = item.get("done", False)
                values[field].append(prepared_subtask)
        elif field == "starPriority":
            values[field] = {1: "yellow", 2: "orange", 3: "red"}.get(raw)
        elif field == "frogSize":
            values[field] = {1: "normal", 2: "baby", 3: "monster"}.get(raw)
        elif field == "dependencies":
            values[field] = [key for key, enabled in (raw or {}).items() if enabled]
        elif field == "snoozedUntil":
            values[field] = (
                None if raw is None else datetime.fromtimestamp(raw / 1000, UTC).isoformat()
            )
        else:
            values[field] = raw if native_name in document else None
    model = RecurringTaskFields if target_type == "recurringTask" else TaskFields
    return model.model_validate(values)


def prepare_draft(
    draft: ChangeDraftV1,
    reader: SnapshotReader,
    *,
    now_ms: int,
) -> ChangePlanV1:
    """Fill only identity, locks, before-state, and review metadata."""

    operations: list[Operation] = []
    for draft_operation in draft.operations:
        target_id = draft_operation.target.id
        resolved_by_title = False
        if draft_operation.action == "create":
            target_id = target_id or str(uuid4())
            document = reader.get_doc(target_id)
            if document is not None:
                raise LivePreconditionError(f"create target ID already exists: {target_id}")
            target = NewItemTarget(type=draft_operation.target.type, id=target_id)
            display = _display_for(None, draft_operation.after, reader, action="create")
            operation: Operation = CreateOperation(
                operationId=draft_operation.operationId,
                action="create",
                target=target,
                after=draft_operation.after,
                reason=draft_operation.reason,
                dependsOnOperations=draft_operation.dependsOnOperations,
                display=display,
            )
        else:
            if target_id is None:
                if draft_operation.target.title is None:
                    raise PlanSemanticError(
                        f"operation {draft_operation.operationId!r} requires target.id or title"
                    )
                target_id = reader.resolve_unique(
                    draft_operation.target.type, draft_operation.target.title
                )
                resolved_by_title = True
            document = reader.get_doc(target_id)
            if document is None:
                raise LivePreconditionError(f"target does not exist: {target_id}")
            found_type = _document_type(document)
            if found_type != draft_operation.target.type:
                raise LivePreconditionError(
                    f"target {target_id!r} is {found_type!r}, not {draft_operation.target.type!r}"
                )
            title = document.get("title")
            if not isinstance(title, str) or not title:
                raise LivePreconditionError(f"target {target_id!r} has no valid title")
            if (
                not resolved_by_title
                and draft_operation.target.title is not None
                and draft_operation.target.title != title
            ):
                raise LivePreconditionError(
                    f"target title is stale: expected {draft_operation.target.title!r}, "
                    f"found {title!r}"
                )
            target = ExistingItemTarget(
                type=draft_operation.target.type,
                id=target_id,
                title=title,
                recurrence=draft_operation.target.recurrence,
            )
            expected_updated_at = document.get("updatedAt")
            if isinstance(expected_updated_at, bool) or not isinstance(expected_updated_at, int):
                expected_updated_at = None
            if draft_operation.action == "update":
                before = _before_fields(target.type, draft_operation.after, document)
                display = _display_for(document, draft_operation.after, reader, action="update")
                operation = UpdateOperation(
                    operationId=draft_operation.operationId,
                    action="update",
                    target=target,
                    before=before,
                    after=draft_operation.after,
                    siblingOrder=draft_operation.siblingOrder,
                    reason=draft_operation.reason,
                    dependsOnOperations=draft_operation.dependsOnOperations,
                    expectedUpdatedAt=expected_updated_at,
                    display=display,
                )
            elif draft_operation.action == "complete":
                display = _display_for(document, None, reader, action="complete")
                completion_day = None
                if target.type == "task":
                    final_day = completion_local_date(draft_operation.completedAt)
                    before_day = usable_marvin_day(document.get("day"))
                    completion_day = CompletionDayTransition(
                        before=before_day,
                        after=final_day,
                        behavior=completion_day_behavior(before_day, final_day),
                    )
                operation = CompleteOperation(
                    operationId=draft_operation.operationId,
                    action="complete",
                    target=target,
                    completedAt=draft_operation.completedAt,
                    completionDay=completion_day,
                    reason=draft_operation.reason,
                    dependsOnOperations=draft_operation.dependsOnOperations,
                    expectedUpdatedAt=expected_updated_at,
                    display=display,
                )
            else:
                display = _display_for(document, None, reader, action="trash")
                operation = TrashOperation(
                    operationId=draft_operation.operationId,
                    action="trash",
                    target=target,
                    reason=draft_operation.reason,
                    dependsOnOperations=draft_operation.dependsOnOperations,
                    expectedUpdatedAt=expected_updated_at,
                    display=display,
                )
        operations.append(operation)
        compiled = compile_operation(operation, document, now_ms)
        reader.put_projected(target_id, project_compiled_mutation(document, compiled))
    prepared = ChangePlanV1(
        schemaVersion=1,
        planId=str(uuid4()),
        createdAt=datetime.fromtimestamp(now_ms / 1000, UTC).isoformat().replace("+00:00", "Z"),
        summary=draft.summary,
        expectedAccount=draft.expectedAccount,
        operations=operations,
    )
    return parse_plan_bytes(canonical_plan_bytes(prepared))


def rebase_plan_live(
    plan: ChangePlanV1,
    reader: SnapshotReader,
    *,
    account_reader: PrepareReader,
    now_ms: int,
) -> ChangePlanV1:
    """Refresh locks and generated display metadata only when reviewed semantics did not drift."""

    verify_expected_account(plan, account_reader)
    value = plan.model_dump(mode="json", by_alias=True, exclude_none=True)
    for index, operation in enumerate(plan.operations):
        if isinstance(operation, CreateOperation):
            if reader.get_doc(operation.target.id) is not None:
                raise LivePreconditionError(
                    f"create operation {operation.operationId!r} target now exists"
                )
            continue
        document = reader.get_doc(operation.target.id)
        if document is None:
            raise LivePreconditionError(f"operation {operation.operationId!r} target disappeared")
        if document.get("title") != operation.target.title:
            raise LivePreconditionError(
                f"operation {operation.operationId!r} title changed; semantic review is required"
            )
        if isinstance(operation, UpdateOperation):
            before = operation.before.model_dump(exclude_unset=True, mode="json")
            for field, expected in before.items():
                matches = (
                    recurring_task_field_matches(field, expected, document)
                    if operation.target.type == "recurringTask"
                    else live_field_matches(field, expected, document)
                )
                if not matches:
                    raise LivePreconditionError(
                        f"operation {operation.operationId!r} field {field!r} changed; "
                        "semantic review is required"
                    )
            display = _display_for(document, operation.after, reader, action="update")
        else:
            display = _display_for(document, None, reader, action=operation.action)
        if isinstance(operation, CompleteOperation) and operation.target.type == "task":
            before_day = usable_marvin_day(document.get("day"))
            if operation.completionDay is not None and operation.completionDay.before != before_day:
                raise LivePreconditionError(
                    f"operation {operation.operationId!r} completion day changed; "
                    "semantic review is required"
                )
            final_day = completion_local_date(operation.completedAt)
            value["operations"][index]["completionDay"] = CompletionDayTransition(
                before=before_day,
                after=final_day,
                behavior=completion_day_behavior(before_day, final_day),
            ).model_dump(mode="json")
        value["operations"][index]["display"] = (
            display.model_dump(mode="json", exclude_none=True) if display is not None else None
        )
        updated_at = document.get("updatedAt")
        value["operations"][index]["expectedUpdatedAt"] = (
            updated_at if isinstance(updated_at, int) and not isinstance(updated_at, bool) else None
        )
    value["createdAt"] = (
        datetime.fromtimestamp(now_ms / 1000, UTC).isoformat().replace("+00:00", "Z")
    )
    # Rebase creates a new immutable artifact and plan ID, never edits the reviewed input.
    value["planId"] = str(uuid4())
    return parse_plan_bytes(json.dumps(value).encode("utf-8"))


def prepared_plan_json(plan: ChangePlanV1) -> str:
    return canonical_plan_bytes(plan).decode("utf-8") + "\n"


def backup_snapshot_reader(path: Path, *, live: PrepareReader | None = None) -> SnapshotReader:
    documents, _format, _bytes, _digest, _cache_hit = load_cached_backup_documents(path)
    return SnapshotReader(documents, live=live)
