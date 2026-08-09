"""Reusable, account-neutral generation and verification for live contract-test plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.models.plan_v1 import LabelRef, ParentRef
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.schema import plan_schema_json

CONTRACT_SUITE_VERSION = 1
SCHEMA_URL = "https://example.invalid/marvin-pilot/v1/schema.json"


class ContractAccountConfig(BaseModel):
    """Optional live-account references needed for complete field/reference coverage."""

    model_config = ConfigDict(extra="forbid")

    sampleOnly: StrictBool = False
    fixturePrefix: StrictStr = "Marvin Pilot Contract"
    parent: ParentRef | None = None
    label: LabelRef | None = None
    customSectionIds: list[StrictStr] = Field(default_factory=list, max_length=2)
    timeBlockSectionIds: list[StrictStr] = Field(default_factory=list, max_length=2)
    nonTaskDocument: ParentRef | None = None

    @field_validator("fixturePrefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        if not value.strip() or len(value) > 200:
            raise ValueError("fixturePrefix must contain 1-200 characters")
        return value

    @field_validator("customSectionIds", "timeBlockSectionIds")
    @classmethod
    def validate_section_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("section ID lists must not contain duplicates")
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("section IDs must contain 1-500 characters")
        return value


def account_config_schema_json() -> str:
    """Return the stable schema for account-reference configuration."""

    schema = ContractAccountConfig.model_json_schema()
    schema["$id"] = "https://example.invalid/marvin-pilot/contract-account-v1.schema.json"
    schema["title"] = "Marvin Pilot contract-test account configuration v1"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_account_config(path: Path | None) -> ContractAccountConfig:
    """Load an optional closed-schema account configuration."""

    if path is None:
        return ContractAccountConfig()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanSyntaxError(f"could not read contract account config {path}: {exc}") from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanSyntaxError(f"contract account config is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanSyntaxError("contract account config root must be a JSON object")
    try:
        return ContractAccountConfig.model_validate(value)
    except ValueError as exc:
        raise PlanSyntaxError(f"invalid contract account config: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ContractCase:
    case_id: str
    filename: str
    plan: dict[str, Any]
    expected_offline: Literal["valid", "invalid"]
    expected_live: str
    matrix: tuple[str, ...]
    notes: str


@dataclass(frozen=True, slots=True)
class ContractSuite:
    manifest: dict[str, Any]
    cases: tuple[ContractCase, ...]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _uuid(run_id: UUID, name: str) -> str:
    return str(uuid5(run_id, name))


def _month_offset(value: date, months: int) -> str:
    absolute = value.year * 12 + value.month - 1 + months
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def _plan(
    run_id: UUID,
    created_at: str,
    name: str,
    summary: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "$schema": SCHEMA_URL,
        "schemaVersion": 1,
        "planId": _uuid(run_id, f"plan:{name}"),
        "createdAt": created_at,
        "summary": summary,
        "operations": operations,
    }


def _create_operation(
    operation_id: str,
    task_id: str,
    title: str,
    reason: str,
    *,
    after: dict[str, Any] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "operationId": operation_id,
        "action": "create",
        "target": {"type": "task", "id": task_id},
        "reason": reason,
        "after": {"title": title, **(after or {})},
    }
    if depends_on:
        value["dependsOnOperations"] = depends_on
    return value


def _update_operation(
    operation_id: str,
    task_id: str,
    title: str,
    reason: str,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    expected_updated_at: int | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "operationId": operation_id,
        "action": "update",
        "target": {"type": "task", "id": task_id, "title": title},
        "reason": reason,
        "before": before,
        "after": after,
    }
    if expected_updated_at is not None:
        value["expectedUpdatedAt"] = expected_updated_at
    return value


def _case(
    case_id: str,
    filename: str,
    plan: dict[str, Any],
    expected_live: str,
    matrix: tuple[str, ...],
    notes: str,
    *,
    expected_offline: Literal["valid", "invalid"] = "valid",
) -> ContractCase:
    return ContractCase(
        case_id,
        filename,
        plan,
        expected_offline,
        expected_live,
        matrix,
        notes,
    )


def generate_contract_suite(
    config: ContractAccountConfig,
    *,
    run_id: UUID,
    created_at: str,
    base_date: date,
    scale_count: int = 200,
    include_limit_cases: bool = False,
) -> ContractSuite:
    """Generate an ordered suite whose live documents are isolated by ``run_id``."""

    if not 1 <= scale_count <= 500:
        raise PlanSemanticError("scale_count must be between 1 and 500")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanSemanticError("created_at must be an RFC 3339 timestamp") from exc
    if parsed_created_at.tzinfo is None:
        raise PlanSemanticError("created_at must include an explicit UTC offset")

    prefix = config.fixturePrefix
    short = str(run_id)[:8]
    title = lambda label: f"{prefix} - {label} - {short}"  # noqa: E731
    reference_id = _uuid(run_id, "task:reference")
    rich_id = _uuid(run_id, "task:rich")
    conflict_id = _uuid(run_id, "task:conflict")
    trash_id = _uuid(run_id, "task:trash")
    anchor = base_date + timedelta(days=(-base_date.weekday()) % 7)
    ds = lambda offset: (anchor + timedelta(days=offset)).isoformat()  # noqa: E731

    initial_rich_title = title("rich field fixture")
    reference_title = title("reference fixture")
    conflict_title = title("conflict fixture")
    trash_title = title("trash fixture")
    cases: list[ContractCase] = []

    create_operations = [
        _create_operation(
            "create-reference-fixture",
            reference_id,
            reference_title,
            "Provide a real task ID for dependency field tests.",
        ),
        _create_operation(
            "create-rich-fixture",
            rich_id,
            initial_rich_title,
            "Provide the isolated target for every supported task field.",
            depends_on=["create-reference-fixture"],
        ),
        _create_operation(
            "create-conflict-fixture",
            conflict_id,
            conflict_title,
            "Provide the isolated target for conflict and unrelated-edit tests.",
            depends_on=["create-rich-fixture"],
        ),
        _create_operation(
            "create-trash-fixture",
            trash_id,
            trash_title,
            "Provide the isolated target for Trash and restore tests.",
            depends_on=["create-conflict-fixture"],
        ),
    ]
    cases.append(
        _case(
            "create-fixtures",
            "01-create-fixtures.json",
            _plan(
                run_id,
                created_at,
                "create-fixtures",
                "Create ordered disposable fixtures for the Marvin Pilot contract suite.",
                create_operations,
            ),
            "apply succeeds; retain receipt for final cleanup",
            ("C02", "C06", "E10", "G02", "G03"),
            "Run first. Revert this receipt last to move every remaining fixture to Trash.",
        )
    )

    negative_specs: list[tuple[str, str, dict[str, Any], tuple[str, ...]]] = [
        (
            "wrong-title",
            "exact target title mismatch",
            _update_operation(
                "reject-wrong-title",
                rich_id,
                title("deliberately wrong title"),
                "Prove the whole plan stops before writes when the target title is stale.",
                {"title": initial_rich_title},
                {"title": title("must never be written")},
            ),
            ("E01",),
        ),
        (
            "stale-before",
            "before-value mismatch",
            _update_operation(
                "reject-stale-before",
                rich_id,
                initial_rich_title,
                "Prove a stale normalized before value blocks all writes.",
                {"note": "value that is not present"},
                {"note": "must never be written"},
            ),
            ("E02",),
        ),
        (
            "stale-updated-at",
            "expectedUpdatedAt mismatch",
            _update_operation(
                "reject-stale-updated-at",
                rich_id,
                initial_rich_title,
                "Prove stale document revision metadata blocks all writes.",
                {"note": None},
                {"note": "must never be written"},
                expected_updated_at=0,
            ),
            ("E03",),
        ),
        (
            "missing-label",
            "missing label reference",
            _update_operation(
                "reject-missing-label",
                rich_id,
                initial_rich_title,
                "Prove missing label references fail live preflight.",
                {"labels": None},
                {"labels": [{"id": f"missing-label-{short}", "title": "Missing label"}]},
            ),
            ("E05",),
        ),
        (
            "missing-parent",
            "missing parent reference",
            _update_operation(
                "reject-missing-parent",
                rich_id,
                initial_rich_title,
                "Prove missing parent references fail live preflight.",
                {"parent": {"id": "unassigned", "title": "Inbox"}},
                {
                    "parent": {
                        "id": _uuid(run_id, "missing:parent"),
                        "title": "Missing parent",
                    }
                },
            ),
            ("E05",),
        ),
        (
            "missing-dependency",
            "missing dependency reference",
            _update_operation(
                "reject-missing-dependency",
                rich_id,
                initial_rich_title,
                "Prove missing dependency references fail live preflight.",
                {"dependencies": None},
                {"dependencies": [_uuid(run_id, "missing:dependency")]},
            ),
            ("E05",),
        ),
        (
            "create-collision",
            "create target already exists",
            _create_operation(
                "reject-create-collision",
                rich_id,
                title("must never replace an existing task"),
                "Prove create refuses to overwrite an existing UUID.",
            ),
            ("C05",),
        ),
    ]
    if config.nonTaskDocument is not None:
        negative_specs.append(
            (
                "non-task",
                "document is not a task",
                _update_operation(
                    "reject-non-task",
                    config.nonTaskDocument.id,
                    config.nonTaskDocument.title or "Configured non-task document",
                    "Prove the v1 task-only boundary rejects category/project documents.",
                    {"note": None},
                    {"note": "must never be written"},
                ),
                ("E04",),
            )
        )
    for index, (slug, expectation, operation, matrix) in enumerate(negative_specs, start=1):
        cases.append(
            _case(
                f"negative-{slug}",
                f"02{chr(96 + index)}-{slug}.json",
                _plan(
                    run_id,
                    created_at,
                    f"negative-{slug}",
                    f"Negative preflight contract: {expectation}.",
                    [operation],
                ),
                "apply exits 5 with zero writes and no mutation receipt",
                matrix,
                "Run after 01 and before 03 while the rich fixture is unchanged.",
            )
        )

    all_before: dict[str, Any] = {
        "title": initial_rich_title,
        "scheduledDate": None,
        "dueDate": None,
        "startDate": None,
        "endDate": None,
        "plannedWeek": None,
        "plannedMonth": None,
        "estimatedTimeDuration": None,
        "note": None,
        "dayRank": None,
        "masterRank": None,
        "dailySection": None,
        "bonusSection": None,
        "starPriority": None,
        "frogSize": None,
        "backburner": None,
        "reviewDate": None,
        "snoozedUntil": None,
        "permanentSnoozeUntil": None,
        "dependencies": None,
    }
    all_after: dict[str, Any] = {
        "title": title("every field set"),
        "scheduledDate": ds(0),
        "dueDate": ds(10),
        "startDate": ds(-1),
        "endDate": ds(8),
        "plannedWeek": ds(0),
        "plannedMonth": _month_offset(anchor, 0),
        "estimatedTimeDuration": "1h30m",
        "note": "Disposable rich-field note for the Marvin Pilot contract suite.",
        "dayRank": 100.5,
        "masterRank": 200.25,
        "dailySection": "Morning",
        "bonusSection": "Essential",
        "starPriority": "yellow",
        "frogSize": "baby",
        "backburner": True,
        "reviewDate": ds(5),
        "snoozedUntil": f"{ds(0)}T10:00:00+00:00",
        "permanentSnoozeUntil": "09:15",
        "dependencies": [reference_id],
    }
    if config.parent is not None:
        all_before["parent"] = {"id": "unassigned", "title": "Inbox"}
        all_after["parent"] = config.parent.model_dump(exclude_none=True)
    if config.label is not None:
        all_before["labels"] = None
        all_after["labels"] = [config.label.model_dump(exclude_none=True)]
    if config.customSectionIds:
        all_before["customSectionId"] = None
        all_after["customSectionId"] = config.customSectionIds[0]
    if config.timeBlockSectionIds:
        all_before["timeBlockSectionId"] = None
        all_after["timeBlockSectionId"] = config.timeBlockSectionIds[0]

    cases.append(
        _case(
            "set-all-fields",
            "03-set-all-fields.json",
            _plan(
                run_id,
                created_at,
                "set-all-fields",
                "Set every configured v1 field on the isolated rich fixture.",
                [
                    _update_operation(
                        "set-every-configured-field",
                        rich_id,
                        initial_rich_title,
                        "Verify public field mappings, metadata timestamps, and live state.",
                        all_before,
                        all_after,
                    )
                ],
            ),
            "apply succeeds",
            tuple(f"D{index:02d}" for index in range(1, 26)),
            "Coverage of parent/label/custom/time-block references follows account config.",
        )
    )

    variant_before = dict(all_after)
    variant_after: dict[str, Any] = {
        "title": title("field variants"),
        "scheduledDate": ds(1),
        "dueDate": ds(11),
        "startDate": ds(0),
        "endDate": ds(9),
        "plannedWeek": ds(7),
        "plannedMonth": _month_offset(anchor, 1),
        "estimatedTimeDuration": "2h",
        "note": "",
        "dayRank": -50.25,
        "masterRank": 0,
        "dailySection": "Afternoon",
        "bonusSection": "Bonus",
        "starPriority": "orange",
        "frogSize": "monster",
        "backburner": False,
        "reviewDate": ds(6),
        "snoozedUntil": f"{ds(1)}T11:30:00+00:00",
        "permanentSnoozeUntil": "18:45",
        "dependencies": None,
    }
    if config.parent is not None:
        variant_after["parent"] = {"id": "unassigned", "title": "Inbox"}
    if config.label is not None:
        variant_after["labels"] = None
    if config.customSectionIds:
        variant_after["customSectionId"] = (
            config.customSectionIds[1] if len(config.customSectionIds) > 1 else None
        )
    if config.timeBlockSectionIds:
        variant_after["timeBlockSectionId"] = (
            config.timeBlockSectionIds[1] if len(config.timeBlockSectionIds) > 1 else None
        )
    cases.append(
        _case(
            "field-variants",
            "04-field-variants.json",
            _plan(
                run_id,
                created_at,
                "field-variants",
                "Exercise alternate values, rescheduling, and empty values.",
                [
                    _update_operation(
                        "set-field-variants",
                        rich_id,
                        str(all_after["title"]),
                        "Verify alternate enum, boolean, text, reference, and schedule values.",
                        variant_before,
                        variant_after,
                    )
                ],
            ),
            "apply succeeds",
            ("D01-D25",),
            "Run immediately after 03; retain receipt for reverse-order cleanup.",
        )
    )

    clear_before = dict(variant_after)
    clear_after = {key: None for key in clear_before if key != "title"}
    clear_after["title"] = title("fields cleared")
    cases.append(
        _case(
            "clear-all-fields",
            "05-clear-all-fields.json",
            _plan(
                run_id,
                created_at,
                "clear-all-fields",
                "Clear every configured optional field and unschedule the rich fixture.",
                [
                    _update_operation(
                        "clear-every-configured-field",
                        rich_id,
                        str(variant_after["title"]),
                        "Verify JSON null clearing and Marvin semantic empty representations.",
                        clear_before,
                        clear_after,
                    )
                ],
            ),
            "apply succeeds",
            ("D03-D24",),
            "Run immediately after 04; verify tracked duration remains untouched.",
        )
    )

    cases.append(
        _case(
            "enum-extremes",
            "06-enum-extremes.json",
            _plan(
                run_id,
                created_at,
                "enum-extremes",
                "Exercise remaining priority, frog, section, and boolean values.",
                [
                    _update_operation(
                        "set-enum-extremes",
                        rich_id,
                        str(clear_after["title"]),
                        "Complete coverage for red star, normal frog, Evening, and true.",
                        {
                            "title": clear_after["title"],
                            "dailySection": None,
                            "starPriority": None,
                            "frogSize": None,
                            "backburner": None,
                        },
                        {
                            "title": title("enum extremes"),
                            "dailySection": "Evening",
                            "starPriority": "red",
                            "frogSize": "normal",
                            "backburner": True,
                        },
                    )
                ],
            ),
            "apply succeeds",
            ("D14", "D18", "D19", "D20"),
            "Revert 06, 05, 04, and 03 in reverse order before reverting 01.",
        )
    )

    conflict_base_title = title("conflict base applied")
    conflict_later_title = title("later touched edit")
    cases.extend(
        [
            _case(
                "conflict-base",
                "07-conflict-base.json",
                _plan(
                    run_id,
                    created_at,
                    "conflict-base",
                    "Establish an applied title for a three-way revert conflict.",
                    [
                        _update_operation(
                            "apply-conflict-base-title",
                            conflict_id,
                            conflict_title,
                            "Establish the applied value in before/applied/current.",
                            {"title": conflict_title},
                            {"title": conflict_base_title},
                        )
                    ],
                ),
                "apply succeeds; later revert is expected to conflict",
                ("F07",),
                "Apply 08 next, then try reverting 07 and expect exit 5 with no write.",
            ),
            _case(
                "conflict-later-edit",
                "08-conflict-later-edit.json",
                _plan(
                    run_id,
                    created_at,
                    "conflict-later-edit",
                    "Change the same touched field after the base apply.",
                    [
                        _update_operation(
                            "apply-later-title-edit",
                            conflict_id,
                            conflict_base_title,
                            "Create a later touched-field value that revert must not clobber.",
                            {"title": conflict_base_title},
                            {"title": conflict_later_title},
                        )
                    ],
                ),
                "apply succeeds",
                ("F07",),
                "After the conflict assertion, revert 08 and then 07 to reset the fixture.",
            ),
        ]
    )

    unrelated_title = title("unrelated edit base")
    cases.extend(
        [
            _case(
                "unrelated-title",
                "09-unrelated-title.json",
                _plan(
                    run_id,
                    created_at,
                    "unrelated-title",
                    "Apply a title-only change before an unrelated note edit.",
                    [
                        _update_operation(
                            "apply-title-before-unrelated-edit",
                            conflict_id,
                            conflict_title,
                            "Prove field-scoped revert preserves an untouched-field edit.",
                            {"title": conflict_title},
                            {"title": unrelated_title},
                        )
                    ],
                ),
                "apply succeeds; revert after 10 also succeeds",
                ("F08",),
                "Apply 10, then revert 09 and confirm the note from 10 survives.",
            ),
            _case(
                "unrelated-note",
                "10-unrelated-note.json",
                _plan(
                    run_id,
                    created_at,
                    "unrelated-note",
                    "Apply a note-only edit unrelated to the earlier title change.",
                    [
                        _update_operation(
                            "apply-unrelated-note-edit",
                            conflict_id,
                            unrelated_title,
                            "Create a legitimate later edit that title revert must preserve.",
                            {"note": None},
                            {"note": f"{prefix} - unrelated note must survive - {short}"},
                        )
                    ],
                ),
                "apply succeeds",
                ("F08",),
                "After reverting 09, revert 10 to restore the note.",
            ),
        ]
    )

    cases.append(
        _case(
            "trash",
            "11-trash.json",
            _plan(
                run_id,
                created_at,
                "trash",
                "Move the isolated Trash fixture into Marvin's reversible UI Trash.",
                [
                    {
                        "operationId": "trash-fixture",
                        "action": "trash",
                        "target": {"type": "task", "id": trash_id, "title": trash_title},
                        "reason": "Verify client-side Trash setters without permanent deletion.",
                    }
                ],
            ),
            "apply succeeds; browser shows Trash; revert restores",
            ("F01", "F02", "F10", "G12"),
            "Run 12 and 13 while trashed, then revert this receipt and verify restoration.",
        )
    )
    cases.extend(
        [
            _case(
                "update-trashed",
                "12-update-trashed.json",
                _plan(
                    run_id,
                    created_at,
                    "update-trashed",
                    "Prove updates to tasks already in Trash are refused.",
                    [
                        _update_operation(
                            "reject-update-trashed",
                            trash_id,
                            trash_title,
                            "Negative Trash-state preflight fixture.",
                            {"note": None},
                            {"note": "must never be written"},
                        )
                    ],
                ),
                "apply exits 5 with zero writes",
                ("E07",),
                "Run only after 11 and before reverting 11.",
            ),
            _case(
                "trash-again",
                "13-trash-again.json",
                _plan(
                    run_id,
                    created_at,
                    "trash-again",
                    "Prove duplicate Trash attempts are refused.",
                    [
                        {
                            "operationId": "reject-trash-again",
                            "action": "trash",
                            "target": {
                                "type": "task",
                                "id": trash_id,
                                "title": trash_title,
                            },
                            "reason": "Negative Trash-state preflight fixture.",
                        }
                    ],
                ),
                "apply exits 5 with zero writes",
                ("E07",),
                "Run only after 11 and before reverting 11.",
            ),
        ]
    )

    ordered_ids = [_uuid(run_id, f"task:ordered:{index}") for index in range(1, 4)]
    ordered_operations = []
    for index, task_id in enumerate(ordered_ids, start=1):
        ordered_operations.append(
            _create_operation(
                f"ordered-create-{index}",
                task_id,
                title(f"ordered fixture {index}"),
                "Verify explicit operation dependency order.",
                depends_on=[f"ordered-create-{index - 1}"] if index > 1 else None,
            )
        )
    cases.append(
        _case(
            "ordered-multi-create",
            "14-ordered-multi-create.json",
            _plan(
                run_id,
                created_at,
                "ordered-multi-create",
                "Create three explicitly ordered fixtures for selective revert.",
                ordered_operations,
            ),
            "apply succeeds; multi-ID selective revert succeeds in reverse order",
            ("C06", "F04", "E10"),
            "Revert two operation IDs in one command, then revert the final operation.",
        )
    )

    stdin_ids = [_uuid(run_id, f"task:stdin:{index}") for index in range(1, 11)]
    stdin_operations = [
        _create_operation(
            f"stdin-create-{index:02d}",
            task_id,
            title(f"stdin fixture {index:02d}"),
            "Exercise stdin durability, progress, pacing, and ETA.",
            depends_on=[f"stdin-create-{index - 1:02d}"] if index > 1 else None,
        )
        for index, task_id in enumerate(stdin_ids, start=1)
    ]
    cases.append(
        _case(
            "stdin-ten-create",
            "15-stdin-ten-create.json",
            _plan(
                run_id,
                created_at,
                "stdin-ten-create",
                "Create ten disposable tasks through stdin for pacing and audit tests.",
                stdin_operations,
            ),
            "piped apply succeeds only with a real controlling TTY",
            ("G05", "G06", "G08"),
            "Pipe file bytes to apply - while confirmation is read from the controlling TTY.",
        )
    )
    update_operations = [
        _update_operation(
            f"paced-update-{index:02d}",
            task_id,
            title(f"stdin fixture {index:02d}"),
            "Exercise sequential multi-task updates and progress reporting.",
            {"note": None},
            {"note": f"{prefix} - paced update {index:02d} - {short}"},
        )
        for index, task_id in enumerate(stdin_ids, start=1)
    ]
    cases.append(
        _case(
            "ten-update",
            "16-ten-update.json",
            _plan(
                run_id,
                created_at,
                "ten-update",
                "Update ten disposable tasks sequentially for pacing and progress tests.",
                update_operations,
            ),
            "apply succeeds",
            ("G06", "G08"),
            "Run after 15; revert 16 before reverting 15.",
        )
    )

    scheduled_rich_id = _uuid(run_id, "task:scheduled-rich")
    rich_create_after = {
        "scheduledDate": ds(2),
        "dueDate": ds(12),
        "startDate": ds(1),
        "endDate": ds(9),
        "plannedWeek": ds(0),
        "plannedMonth": _month_offset(anchor, 0),
        "estimatedTimeDuration": "45m",
        "note": "Disposable rich-create note for the Marvin Pilot contract suite.",
        "dayRank": 1.5,
        "masterRank": 2.5,
        "dailySection": "Morning",
        "bonusSection": "Essential",
        "starPriority": "yellow",
        "frogSize": "baby",
        "backburner": False,
        "reviewDate": ds(6),
        "snoozedUntil": f"{ds(2)}T12:00:00+00:00",
        "permanentSnoozeUntil": "12:30",
        "dependencies": [reference_id],
    }
    if config.parent is not None:
        rich_create_after["parent"] = config.parent.model_dump(exclude_none=True)
    if config.label is not None:
        rich_create_after["labels"] = [config.label.model_dump(exclude_none=True)]
    if config.customSectionIds:
        rich_create_after["customSectionId"] = config.customSectionIds[0]
    if config.timeBlockSectionIds:
        rich_create_after["timeBlockSectionId"] = config.timeBlockSectionIds[0]
    cases.append(
        _case(
            "scheduled-rich-create",
            "17-scheduled-rich-create.json",
            _plan(
                run_id,
                created_at,
                "scheduled-rich-create",
                "Create a scheduled task with every configured create-time field.",
                [
                    _create_operation(
                        "create-scheduled-rich-fixture",
                        scheduled_rich_id,
                        title("scheduled rich fixture"),
                        "Verify create shape, firstScheduled, fields, and create inverse.",
                        after=rich_create_after,
                    )
                ],
            ),
            "apply succeeds; revert moves created task to UI Trash",
            ("C03", "C04", "C07", "F09", "G12"),
            "Verify firstScheduled in API/browser state, then revert the apply receipt.",
        )
    )

    scale_operations = []
    for index in range(1, scale_count + 1):
        scale_operations.append(
            _create_operation(
                f"scale-create-{index:03d}",
                _uuid(run_id, f"task:scale:{scale_count}:{index}"),
                title(f"scale {scale_count} fixture {index:03d}"),
                "Exercise sustainable sequential execution and durable audit history.",
                depends_on=[f"scale-create-{index - 1:03d}"] if index > 1 else None,
            )
        )
    cases.append(
        _case(
            "scale-create",
            f"18-scale-{scale_count}-create.json",
            _plan(
                run_id,
                created_at,
                f"scale-{scale_count}",
                f"Create {scale_count} disposable tasks for scale and rate-limit testing.",
                scale_operations,
            ),
            "apply and complete/partial-resume revert ultimately confirm every operation",
            ("G07", "G09", "G11"),
            "Record elapsed time, response failures, retry gaps, receipts, and aggregate cleanup.",
        )
    )

    if include_limit_cases:
        for count, expected in ((500, "valid"), (501, "invalid")):
            operations = [
                _create_operation(
                    f"limit-create-{index:03d}",
                    _uuid(run_id, f"task:limit:{count}:{index}"),
                    title(f"offline limit {count} fixture {index:03d}"),
                    "Exercise the offline maximum-operation schema boundary.",
                )
                for index in range(1, count + 1)
            ]
            cases.append(
                _case(
                    f"offline-limit-{count}",
                    f"19{'a' if count == 500 else 'b'}-offline-limit-{count}.json",
                    _plan(
                        run_id,
                        created_at,
                        f"offline-limit-{count}",
                        f"Exercise offline validation at {count} operations.",
                        operations,
                    ),
                    "do not apply; offline validation only",
                    ("G10",),
                    "The 500 case validates; the 501 case must fail schema validation.",
                    expected_offline=expected,  # type: ignore[arg-type]
                )
            )

    coverage = {
        "parent": config.parent is not None,
        "label": config.label is not None,
        "customSectionId": bool(config.customSectionIds),
        "timeBlockSectionId": bool(config.timeBlockSectionIds),
        "nonTaskBoundary": config.nonTaskDocument is not None,
    }
    manifest_cases = []
    for item in cases:
        raw = _json_bytes(item.plan)
        manifest_cases.append(
            {
                "caseId": item.case_id,
                "file": item.filename,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "expectedOfflineValidation": item.expected_offline,
                "expectedLiveResult": item.expected_live,
                "matrix": list(item.matrix),
                "notes": item.notes,
            }
        )
    manifest = {
        "suiteVersion": CONTRACT_SUITE_VERSION,
        "runId": str(run_id),
        "createdAt": created_at,
        "baseDate": base_date.isoformat(),
        "sampleOnly": config.sampleOnly,
        "fixturePrefix": config.fixturePrefix,
        "scaleCount": scale_count,
        "coverage": coverage,
        "schemas": {
            "changePlan": {
                "file": "change-plan-v1.schema.json",
                "sha256": hashlib.sha256(plan_schema_json().encode()).hexdigest(),
            },
            "accountConfig": {
                "file": "account-config-v1.schema.json",
                "sha256": hashlib.sha256(account_config_schema_json().encode()).hexdigest(),
            },
        },
        "cases": manifest_cases,
        "workflow": [
            "Validate the suite offline before loading any credential.",
            "Apply 01, then run every 02 negative case before mutating the rich fixture.",
            "Apply 03-06 in order; revert 06-03 in reverse order after inspecting each state.",
            "Run 07/08 conflict and 09/10 unrelated-field workflows exactly as their notes say.",
            "Apply 11, assert 12/13 fail, then revert 11 to verify restore.",
            "Run and clean up 14-18; use repeated --only IDs for the selective-revert case.",
            "Finally revert receipt 01 so all remaining base fixtures move to Marvin Trash.",
        ],
    }
    return ContractSuite(manifest=manifest, cases=tuple(cases))


def write_contract_suite(output: Path, suite: ContractSuite) -> None:
    """Write a new suite directory without overwriting existing content."""

    if output.exists():
        try:
            occupied = not output.is_dir() or any(output.iterdir())
        except OSError as exc:
            raise PlanSyntaxError(f"could not inspect output directory {output}: {exc}") from exc
        if occupied:
            raise PlanSyntaxError(f"refusing to overwrite non-empty output directory: {output}")
    try:
        output.mkdir(parents=True, exist_ok=True)
        (output / "change-plan-v1.schema.json").write_text(
            plan_schema_json(), encoding="utf-8", newline="\n"
        )
        (output / "account-config-v1.schema.json").write_text(
            account_config_schema_json(), encoding="utf-8", newline="\n"
        )
        for case in suite.cases:
            (output / case.filename).write_bytes(_json_bytes(case.plan))
        (output / "manifest.json").write_bytes(_json_bytes(suite.manifest))
    except OSError as exc:
        raise PlanSyntaxError(f"could not write contract suite {output}: {exc}") from exc


def verify_contract_suite(path: Path) -> tuple[int, int]:
    """Verify manifest hashes and each case's expected offline validity."""

    try:
        manifest = json.loads((path / "manifest.json").read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanSyntaxError(f"could not read contract suite manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("suiteVersion") != CONTRACT_SUITE_VERSION:
        raise PlanSyntaxError("unsupported or malformed contract suite manifest")
    schemas = manifest.get("schemas")
    if not isinstance(schemas, dict) or set(schemas) != {"changePlan", "accountConfig"}:
        raise PlanSyntaxError("contract suite manifest has invalid schema metadata")
    for entry in schemas.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise PlanSyntaxError("contract suite manifest has malformed schema metadata")
        try:
            raw = (path / entry["file"]).read_bytes()
        except OSError as exc:
            raise PlanSyntaxError(f"could not read contract schema {entry['file']}: {exc}") from exc
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise PlanSyntaxError(f"contract schema hash mismatch: {entry['file']}")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise PlanSyntaxError("contract suite manifest has no cases")

    valid = 0
    invalid = 0
    for entry in cases:
        if not isinstance(entry, dict):
            raise PlanSyntaxError("contract suite manifest contains a malformed case")
        filename = entry.get("file")
        expected = entry.get("expectedOfflineValidation")
        expected_hash = entry.get("sha256")
        if not isinstance(filename, str) or expected not in {"valid", "invalid"}:
            raise PlanSyntaxError("contract suite manifest contains invalid case metadata")
        try:
            raw = (path / filename).read_bytes()
        except OSError as exc:
            raise PlanSyntaxError(f"could not read contract case {filename}: {exc}") from exc
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise PlanSyntaxError(f"contract case hash mismatch: {filename}")
        try:
            parse_plan_bytes(raw)
        except (PlanSyntaxError, PlanSemanticError):
            if expected != "invalid":
                raise PlanSyntaxError(f"expected valid contract case failed: {filename}") from None
            invalid += 1
        else:
            if expected != "valid":
                raise PlanSyntaxError(f"expected invalid contract case passed: {filename}")
            valid += 1
    return valid, invalid
