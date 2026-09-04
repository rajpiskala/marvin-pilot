from __future__ import annotations

import json

from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import parse_plan_bytes
from marvin_pilot.schema import draft_schema_json, plan_schema_json, plan_set_schema_json


def test_generated_schema_is_json_and_closed() -> None:
    schema = json.loads(plan_schema_json())
    assert schema["title"] == "Marvin Pilot change plan v1"
    assert schema["additionalProperties"] is False
    assert all(
        definition.get("additionalProperties") is False for definition in schema["$defs"].values()
    )


def test_embedded_example_stays_valid() -> None:
    plan = parse_plan_bytes(json.dumps(EXAMPLE_PLAN).encode())
    assert [operation.action for operation in plan.operations] == [
        "update",
        "update",
        "create",
        "trash",
    ]


def test_compact_draft_and_plan_set_schemas_are_closed() -> None:
    draft = json.loads(draft_schema_json())
    plan_set = json.loads(plan_set_schema_json())
    assert draft["title"] == "Marvin Pilot compact draft v1"
    assert plan_set["title"] == "Marvin Pilot plan set v1"
    assert draft["additionalProperties"] is False
    assert plan_set["additionalProperties"] is False
