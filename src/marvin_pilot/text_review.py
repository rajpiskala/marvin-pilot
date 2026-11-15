"""Identity-bound text annotations for AI reconciliation, never an executable plan."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError
from marvin_pilot.models.plan_v1 import ChangePlanV1
from marvin_pilot.plan_io import plan_digest


class TextEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operationId: StrictStr
    field: Literal["title", "subtaskTitle"]
    subtaskId: StrictStr | None = None
    text: StrictStr = Field(min_length=1, max_length=4000)


def text_choices(plan: ChangePlanV1) -> list[dict[str, Any]]:
    choices = []
    for operation in plan.model_dump(mode="json", exclude_unset=True)["operations"]:
        if operation["action"] == "trash":
            continue
        before, after = operation.get("before") or {}, operation.get("after") or {}
        title = after.get("title", before.get("title", operation["target"].get("title")))
        base = {
            "operationId": operation["operationId"],
            "targetId": operation["target"]["id"],
            "targetType": operation["target"]["type"],
            "action": operation["action"],
        }
        if isinstance(title, str):
            choices.append({**base, "field": "title", "subtaskId": None, "originalText": title})
        for subtask in after.get("subtasks", before.get("subtasks", [])) or []:
            choices.append(
                {
                    **base,
                    "field": "subtaskTitle",
                    "subtaskId": subtask["id"],
                    "originalText": subtask["title"],
                }
            )
    return choices


def build_text_review(
    plan: ChangePlanV1,
    edits: list[dict[str, Any]],
    *,
    context: dict[str, Any],
    allow_original: bool = False,
) -> dict[str, Any]:
    choices = {(c["operationId"], c["field"], c["subtaskId"]): c for c in text_choices(plan)}
    if not edits or len(edits) > len(choices):
        raise PlanSemanticError("supply changed title/subtask text, not structural edits")
    if set(context) != {"timeZone", "localDate"}:
        raise PlanSemanticError("text review requires the browser's timeZone and localDate")
    if not isinstance(context["timeZone"], str) or not 0 < len(context["timeZone"]) <= 128:
        raise PlanSemanticError("invalid reported time zone")
    try:
        date.fromisoformat(context["localDate"])
    except (ValueError, TypeError) as exc:
        raise PlanSemanticError("invalid local reference date") from exc
    result, seen = [], set()
    for raw in edits:
        try:
            edit = TextEdit.model_validate(raw)
        except ValidationError as exc:
            raise PlanSyntaxError(f"invalid text-only edit: {exc}") from exc
        key = (edit.operationId, edit.field, edit.subtaskId)
        if key not in choices or key in seen:
            raise PlanSemanticError("unknown or duplicate title/subtask identity")
        seen.add(key)
        choice = choices[key]
        if not edit.text.strip():
            raise PlanSemanticError("titles cannot be removed; edit the text instead")
        if edit.text == choice["originalText"] and not allow_original:
            raise PlanSemanticError("text edit does not change the proposal")
        result.append({**choice, "text": edit.text})
    return {
        "reviewVersion": 1,
        "kind": "marvin-pilot-text-review",
        "reviewId": str(uuid4()),
        "sourcePlanId": plan.planId,
        "sourceDigest": plan_digest(plan),
        "reviewedAt": datetime.now(UTC).isoformat(),
        "referenceContext": context,
        "requiresAIReconciliation": True,
        "edits": result,
    }
