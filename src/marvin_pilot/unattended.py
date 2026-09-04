"""Bounded policy checks for explicitly enabled unattended apply."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from marvin_pilot.errors import PlanSemanticError
from marvin_pilot.models.plan_v1 import (
    ChangePlanV1,
    CreateOperation,
    TrashOperation,
    UpdateOperation,
)
from marvin_pilot.preflight import PreflightOperation, PreflightResult


@dataclass(frozen=True, slots=True)
class UnattendedAssessment:
    """Effective item impact and operations that require human review."""

    impact: int
    blockers: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.blockers


def _subtask_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(identifier) for identifier in value}
    if not isinstance(value, list):
        return set()
    identifiers: set[str] = set()
    for index, item in enumerate(value):
        identifier = item.get("id") or item.get("_id") if isinstance(item, dict) else None
        identifiers.add(str(identifier) if identifier is not None else f"<position:{index}>")
    return identifiers


def _subtask_impact(checked: PreflightOperation) -> int:
    operation = checked.operation
    if operation.target.type != "task":
        return 0
    live_ids = _subtask_ids((checked.live_document or {}).get("subtasks"))
    if isinstance(operation, TrashOperation):
        return len(live_ids)
    if not isinstance(operation, (CreateOperation, UpdateOperation)):
        return 0
    after = operation.after.model_dump(exclude_unset=True, mode="json")
    if "subtasks" not in after:
        return 0
    return len(live_ids | _subtask_ids(after["subtasks"]))


def unattended_plan_blockers(plan: ChangePlanV1) -> tuple[str, ...]:
    """Identify operation types whose effect is not safely bounded by explicit item count."""

    blockers: list[str] = []
    for operation in plan.operations:
        if operation.target.type == "recurringTask":
            blockers.append(
                f"[{operation.operationId}] targets a recurring-task series; series changes "
                "require interactive review"
            )
        if isinstance(operation, TrashOperation) and operation.target.type == "project":
            blockers.append(
                f"[{operation.operationId}] trashes a project; project deletion requires "
                "interactive review"
            )
    return tuple(blockers)


def blocked_unattended_error(blockers: tuple[str, ...]) -> PlanSemanticError:
    """Render all static unattended-policy blockers as one actionable error."""

    details = "\n".join(f"- {message}" for message in blockers)
    return PlanSemanticError("unattended apply requires interactive review:\n" + details)


def assess_unattended(preflight: PreflightResult) -> UnattendedAssessment:
    """Measure explicit document/subtask impact and identify unbounded container actions."""

    impact = len(preflight.operations)
    for checked in preflight.operations:
        impact += _subtask_impact(checked)
    return UnattendedAssessment(
        impact=impact,
        blockers=unattended_plan_blockers(preflight.plan),
    )


def enforce_unattended(
    preflight: PreflightResult, *, max_impact: int
) -> UnattendedAssessment:
    """Fail closed when a preflight result exceeds its human-configured policy."""

    assessment = assess_unattended(preflight)
    if assessment.blockers:
        raise blocked_unattended_error(assessment.blockers)
    if assessment.impact > max_impact:
        raise PlanSemanticError(
            f"plan impact is {assessment.impact}; configured unattended maximum is {max_impact}"
        )
    return assessment
