"""JSON Schema generation for authoring tools and LLMs."""

from __future__ import annotations

import json

from marvin_pilot.models.plan_v1 import ChangePlanV1
from marvin_pilot.plan_set import PlanSetV1
from marvin_pilot.prepare import ChangeDraftV1


def _schema_json(model: type, identifier: str, title: str) -> str:
    schema = model.model_json_schema(by_alias=True)
    schema["$id"] = identifier
    schema["title"] = title
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def plan_schema_json() -> str:
    """Return the embedded v1 model schema as stable, pretty JSON."""

    return _schema_json(
        ChangePlanV1,
        "https://example.invalid/marvin-pilot/v1/schema.json",
        "Marvin Pilot change plan v1",
    )


def draft_schema_json() -> str:
    """Return the strict compact-authoring draft schema."""

    return _schema_json(
        ChangeDraftV1,
        "https://example.invalid/marvin-pilot/v1/draft-schema.json",
        "Marvin Pilot compact draft v1",
    )


def plan_set_schema_json() -> str:
    """Return the dependency-ordered plan-set manifest schema."""

    return _schema_json(
        PlanSetV1,
        "https://example.invalid/marvin-pilot/v1/plan-set-schema.json",
        "Marvin Pilot plan set v1",
    )
