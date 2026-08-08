from __future__ import annotations

import json
from pathlib import Path

import pytest

from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import (
    MAX_PLAN_BYTES,
    canonical_plan_bytes,
    load_plan,
    parse_plan_bytes,
    plan_digest,
)


def encode(value: object) -> bytes:
    return json.dumps(value).encode()


def test_example_is_valid_and_preserves_explicit_nulls() -> None:
    plan = parse_plan_bytes(encode(EXAMPLE_PLAN))

    dinner = plan.operations[1]
    assert dinner.before.model_fields_set == {"title", "estimatedTimeDuration", "parent"}
    assert dinner.before.estimatedTimeDuration is None
    assert len(plan.operations) == 4


def test_canonical_digest_is_independent_of_json_key_order() -> None:
    forward = parse_plan_bytes(encode(EXAMPLE_PLAN))
    reversed_top_level = dict(reversed(list(EXAMPLE_PLAN.items())))
    reverse = parse_plan_bytes(encode(reversed_top_level))

    assert canonical_plan_bytes(forward) == canonical_plan_bytes(reverse)
    assert plan_digest(forward) == plan_digest(reverse)
    assert plan_digest(forward).startswith("sha256:")
    assert len(plan_digest(forward)) == len("sha256:") + 64


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b"{} // comment",
        b'{"schemaVersion": 1,}',
        b'{"schemaVersion": NaN}',
        b'{"schemaVersion": Infinity}',
        b"\xff",
    ],
)
def test_non_strict_json_is_rejected(raw: bytes) -> None:
    with pytest.raises(PlanSyntaxError):
        parse_plan_bytes(raw)


def test_duplicate_json_keys_are_rejected_at_any_depth() -> None:
    raw = b"""{
      "schemaVersion": 1,
      "schemaVersion": 1,
      "planId": "5f97946d-35f3-4a52-84de-4fe51e694788"
    }"""
    with pytest.raises(PlanSyntaxError, match="duplicate JSON object key"):
        parse_plan_bytes(raw)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p.update({"invented": True}), "Extra inputs are not permitted"),
        (
            lambda p: p["operations"][0]["after"].update({"duration": "30m"}),
            "Extra inputs are not permitted",
        ),
        (lambda p: p.update({"schemaVersion": True}), "integer 1"),
        (lambda p: p.update({"schemaVersion": 1.0}), "integer 1"),
        (lambda p: p.update({"createdAt": "2026-08-08T14:45:00"}), "explicit UTC offset"),
        (
            lambda p: p["operations"][0]["after"].update({"scheduledDate": "08/09/2026"}),
            "YYYY-MM-DD",
        ),
        (
            lambda p: p["operations"][1]["after"].update({"estimatedTimeDuration": "none"}),
            "invalid estimatedTimeDuration",
        ),
        (
            lambda p: p["operations"][2]["target"].update({"id": "not-a-uuid"}),
            "must be a UUID",
        ),
    ],
)
def test_schema_errors_are_actionable(example_plan_dict: dict, mutate, message: str) -> None:
    mutate(example_plan_dict)
    with pytest.raises(PlanSyntaxError, match=message):
        parse_plan_bytes(encode(example_plan_dict))


def test_duration_is_canonicalized(example_plan_dict: dict) -> None:
    example_plan_dict["operations"][1]["after"]["estimatedTimeDuration"] = "270m"
    plan = parse_plan_bytes(encode(example_plan_dict))
    assert plan.operations[1].after.estimatedTimeDuration == "4h30m"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("title", "   ", "must not be empty"),
        ("dueDate", "2026-02-30", "real calendar date"),
        ("plannedWeek", "2026-08-08", "must be a Monday"),
        ("plannedMonth", "2026-13", "real calendar month"),
        ("plannedMonth", "2026/08", "must use YYYY-MM"),
        ("snoozedUntil", "2026-08-08T12:00:00", "explicit UTC offset"),
        ("permanentSnoozeUntil", "7:30", "must use HH:mm"),
        ("customSectionId", "", "must not be empty"),
        ("timeBlockSectionId", " ", "must not be empty"),
        ("dayRank", "1", "valid number"),
        ("backburner", 1, "valid boolean"),
    ],
)
def test_task_field_validators(
    example_plan_dict: dict, field: str, value: object, message: str
) -> None:
    operation = example_plan_dict["operations"][0]
    operation["before"] = {field: None}
    operation["after"] = {field: value}
    with pytest.raises(PlanSyntaxError, match=message):
        parse_plan_bytes(encode(example_plan_dict))


def test_note_size_limit(example_plan_dict: dict) -> None:
    operation = example_plan_dict["operations"][0]
    operation["before"] = {"note": None}
    operation["after"] = {"note": "x" * 200_001}
    with pytest.raises(PlanSyntaxError, match="at most 200000"):
        parse_plan_bytes(encode(example_plan_dict))


def test_duplicate_label_ids_are_rejected(example_plan_dict: dict) -> None:
    labels = [{"id": "label-a"}, {"id": "label-a", "title": "Duplicate"}]
    operation = example_plan_dict["operations"][0]
    operation["before"] = {"labels": None}
    operation["after"] = {"labels": labels}
    with pytest.raises(PlanSyntaxError, match="unique IDs"):
        parse_plan_bytes(encode(example_plan_dict))


@pytest.mark.parametrize(
    "dependencies",
    [["task-a", "task-a"], ["task-a", ""]],
)
def test_task_dependencies_are_validated(example_plan_dict: dict, dependencies: list[str]) -> None:
    operation = example_plan_dict["operations"][0]
    operation["before"] = {"dependencies": None}
    operation["after"] = {"dependencies": dependencies}
    with pytest.raises(PlanSyntaxError):
        parse_plan_bytes(encode(example_plan_dict))


def test_parent_and_label_hints_must_not_be_empty(example_plan_dict: dict) -> None:
    operation = example_plan_dict["operations"][0]
    operation["before"] = {"parent": None, "labels": None}
    operation["after"] = {
        "parent": {"id": "", "title": " "},
        "labels": [{"id": "", "title": " "}],
    }
    with pytest.raises(PlanSyntaxError, match="must not be empty"):
        parse_plan_bytes(encode(example_plan_dict))


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("planId",), "not-a-uuid", "planId must be a UUID"),
        (("summary",), " ", "summary must not be empty"),
        (("$schema",), "http://example.com/schema.json", "must be an HTTPS URL"),
        (("operations", 0, "operationId"), "Bad_ID", "lowercase letters"),
        (("operations", 0, "reason"), "", "reason must not be empty"),
        (("operations", 0, "target", "id"), "", "target.id must not be empty"),
        (("operations", 0, "target", "title"), "", "target.title must not be empty"),
    ],
)
def test_identity_and_prose_validation(
    example_plan_dict: dict, path: tuple, value: object, message: str
) -> None:
    current = example_plan_dict
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = value
    with pytest.raises(PlanSyntaxError, match=message):
        parse_plan_bytes(encode(example_plan_dict))


def test_create_requires_title(example_plan_dict: dict) -> None:
    del example_plan_dict["operations"][2]["after"]["title"]
    with pytest.raises(PlanSyntaxError, match="title: Field required"):
        parse_plan_bytes(encode(example_plan_dict))


def test_operation_dependencies_reject_duplicates_and_invalid_ids(example_plan_dict: dict) -> None:
    example_plan_dict["operations"][1]["dependsOnOperations"] = ["bad_ID", "bad_ID"]
    with pytest.raises(PlanSyntaxError, match="unique operation IDs"):
        parse_plan_bytes(encode(example_plan_dict))


def test_update_key_sets_must_match(example_plan_dict: dict) -> None:
    del example_plan_dict["operations"][0]["before"]["scheduledDate"]
    with pytest.raises(PlanSemanticError, match="identical before/after"):
        parse_plan_bytes(encode(example_plan_dict))


def test_update_must_include_a_field(example_plan_dict: dict) -> None:
    example_plan_dict["operations"][0]["before"] = {}
    example_plan_dict["operations"][0]["after"] = {}
    with pytest.raises(PlanSemanticError, match="at least one field"):
        parse_plan_bytes(encode(example_plan_dict))


def test_update_must_change_a_value(example_plan_dict: dict) -> None:
    example_plan_dict["operations"][0]["after"] = dict(example_plan_dict["operations"][0]["before"])
    with pytest.raises(PlanSemanticError, match="does not change"):
        parse_plan_bytes(encode(example_plan_dict))


def test_duplicate_operation_ids_are_rejected(example_plan_dict: dict) -> None:
    example_plan_dict["operations"][1]["operationId"] = example_plan_dict["operations"][0][
        "operationId"
    ]
    with pytest.raises(PlanSemanticError, match="duplicate operationId"):
        parse_plan_bytes(encode(example_plan_dict))


def test_duplicate_target_ids_are_rejected(example_plan_dict: dict) -> None:
    example_plan_dict["operations"][1]["target"]["id"] = example_plan_dict["operations"][0][
        "target"
    ]["id"]
    with pytest.raises(PlanSemanticError, match="more than one operation targets"):
        parse_plan_bytes(encode(example_plan_dict))


def test_dependencies_must_refer_to_earlier_operations(example_plan_dict: dict) -> None:
    example_plan_dict["operations"][0]["dependsOnOperations"] = ["improve-dinner-task"]
    with pytest.raises(PlanSemanticError, match="must be an earlier operation"):
        parse_plan_bytes(encode(example_plan_dict))


def test_earlier_operation_dependency_is_valid(example_plan_dict: dict) -> None:
    example_plan_dict["operations"][1]["dependsOnOperations"] = ["reschedule-wash-dishes"]
    parse_plan_bytes(encode(example_plan_dict))


def test_plan_size_limit() -> None:
    with pytest.raises(PlanSyntaxError, match="exceeds"):
        parse_plan_bytes(b" " * (MAX_PLAN_BYTES + 1))


def test_load_plan_requires_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises(PlanSyntaxError, match="not a regular file"):
        load_plan(tmp_path)


def test_load_plan_reads_exact_input_bytes(tmp_path: Path) -> None:
    raw = json.dumps(EXAMPLE_PLAN, indent=3).encode()
    path = tmp_path / "plan.json"
    path.write_bytes(raw)
    plan, loaded = load_plan(path)
    assert plan.planId == EXAMPLE_PLAN["planId"]
    assert loaded == raw
