"""Read-only hierarchy metadata used to enrich an offline visualizer preview."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

HierarchyNodeType = Literal["category", "project", "task"]
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


@dataclass(frozen=True, slots=True)
class HierarchyNode:
    id: str
    type: HierarchyNodeType
    title: str
    parent_id: str | None
    emoji: str | None
    color: str | None
    order: int | None


@dataclass(frozen=True, slots=True)
class HierarchyContext:
    """A compact active-item index; raw backup documents never reach the browser."""

    nodes: dict[str, HierarchyNode]
    input_document_count: int


def _node_type(document: dict[str, Any]) -> HierarchyNodeType | None:
    if document.get("db") == "Categories" and document.get("type") in {
        "category",
        "project",
    }:
        return document["type"]
    if document.get("db") == "Tasks":
        return "task"
    return None


def _updated_at(document: dict[str, Any]) -> float:
    value = document.get("updatedAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return -math.inf
    return float(value) if math.isfinite(value) else -math.inf


def _active(document: dict[str, Any]) -> bool:
    deleted_at = document.get("deletedAt")
    return not document.get("_deleted") and (
        deleted_at is None or deleted_at is False or deleted_at == "" or deleted_at == 0
    )


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _order(document: dict[str, Any]) -> int | None:
    for field in ("rank", "masterRank"):
        value = document.get(field)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def build_backup_hierarchy_context(documents: list[dict[str, Any]]) -> HierarchyContext:
    """Index active Marvin containers/tasks, resolving duplicate IDs by latest updatedAt."""

    selected: dict[str, dict[str, Any]] = {}
    for document in documents:
        identifier = document.get("_id")
        title = document.get("title")
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(title, str)
            or not title
            or _node_type(document) is None
        ):
            continue
        previous = selected.get(identifier)
        if previous is None or _updated_at(document) > _updated_at(previous):
            selected[identifier] = document

    nodes: dict[str, HierarchyNode] = {}
    for identifier, document in selected.items():
        if not _active(document):
            continue
        node_type = _node_type(document)
        assert node_type is not None
        parent_id = document.get("parentId")
        if not isinstance(parent_id, str) or not parent_id:
            parent_id = "unassigned"
        color = _optional_text(document.get("color"))
        if color is not None and not _HEX_COLOR.fullmatch(color):
            color = None
        nodes[identifier] = HierarchyNode(
            id=identifier,
            type=node_type,
            title=document["title"],
            parent_id=parent_id,
            emoji=_optional_text(document.get("emoji")),
            color=color.lower() if color is not None else None,
            order=_order(document),
        )
    return HierarchyContext(nodes=nodes, input_document_count=len(documents))
