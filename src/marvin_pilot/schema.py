"""JSON Schema generation for authoring tools and LLMs."""

from __future__ import annotations

import json

from marvin_pilot.models.plan_v1 import ChangePlanV1


def plan_schema_json() -> str:
    """Return the embedded v1 model schema as stable, pretty JSON."""

    schema = ChangePlanV1.model_json_schema(by_alias=True)
    schema["$id"] = "https://example.invalid/marvin-pilot/v1/schema.json"
    schema["title"] = "Marvin Pilot change plan v1"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
