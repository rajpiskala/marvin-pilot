"""Read-only, compact project context extracted from a Marvin JSON backup."""

from __future__ import annotations

import json
import lzma
import math
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from marvin_pilot.errors import PlanSemanticError, PlanSyntaxError

MAX_BACKUP_FILE_BYTES = 256 * 1024 * 1024
MAX_DECOMPRESSED_BACKUP_BYTES = 256 * 1024 * 1024
SUPPORTED_DATABASES = frozenset({"Categories", "Tasks", "RecurringTasks"})
ContextItemType = Literal["category", "project", "task", "recurringTask"]


def _read_limited(stream: Any, limit: int) -> bytes:
    value = stream.read(limit + 1)
    if len(value) > limit:
        raise PlanSyntaxError(f"decompressed Marvin backup exceeds the {limit}-byte safety limit")
    return value


def _normalize_surrogate_pairs(value: str) -> str:
    """Combine CESU-8 surrogate pairs while rejecting corrupt lone surrogates."""

    result: list[str] = []
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= len(value):
                raise PlanSyntaxError("Marvin backup contains a truncated CESU-8 character")
            low = ord(value[index + 1])
            if not 0xDC00 <= low <= 0xDFFF:
                raise PlanSyntaxError("Marvin backup contains an invalid CESU-8 surrogate pair")
            result.append(chr(0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)))
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            raise PlanSyntaxError("Marvin backup contains an invalid lone CESU-8 surrogate")
        result.append(value[index])
        index += 1
    return "".join(result)


def _decode_backup(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        try:
            decoded = value.decode("utf-8", errors="surrogatepass")
        except UnicodeDecodeError as exc:
            raise PlanSyntaxError(f"Marvin backup is not UTF-8 or CESU-8: {exc}") from exc
        return _normalize_surrogate_pairs(decoded)


def load_backup_documents(path: Path) -> tuple[list[dict[str, Any]], str, int]:
    """Load a plain or LZMA-compressed Marvin backup without writing an expanded copy."""

    try:
        if not path.is_file():
            raise PlanSyntaxError(f"backup path is not a regular file: {path}")
        size = path.stat().st_size
        if size > MAX_BACKUP_FILE_BYTES:
            raise PlanSyntaxError(
                f"Marvin backup exceeds the {MAX_BACKUP_FILE_BYTES}-byte input safety limit"
            )
        compressed = path.name.lower().endswith(".lzma")
        if compressed:
            with lzma.open(path, "rb") as stream:
                raw = _read_limited(stream, MAX_DECOMPRESSED_BACKUP_BYTES)
            source_format = "marvin-backup-json-lzma"
        else:
            with path.open("rb") as stream:
                raw = _read_limited(stream, MAX_DECOMPRESSED_BACKUP_BYTES)
            source_format = "marvin-backup-json"
    except PlanSyntaxError:
        raise
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise PlanSyntaxError(f"could not read Marvin backup {path}: {exc}") from exc

    try:
        value = json.loads(_decode_backup(raw))
    except json.JSONDecodeError as exc:
        raise PlanSyntaxError(
            f"Marvin backup is not valid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if isinstance(value, dict) and isinstance(value.get("docs"), list):
        value = value["docs"]
    if not isinstance(value, list):
        raise PlanSyntaxError("Marvin backup root must be a JSON array or an object with docs[]")
    if not all(isinstance(document, dict) for document in value):
        raise PlanSyntaxError("every Marvin backup document must be a JSON object")
    return value, source_format, size


def _item_type(document: dict[str, Any]) -> ContextItemType | None:
    database = document.get("db")
    if database == "Categories" and document.get("type") in {"category", "project"}:
        return document["type"]
    if database == "Tasks":
        return "task"
    if database == "RecurringTasks" and document.get("recurringType") == "task":
        return "recurringTask"
    return None


def _meaningful(value: Any) -> bool:
    if isinstance(value, str):
        return value not in {"", "unassigned"}
    return value is not None and value is not False and value != 0


def _is_trashed(document: dict[str, Any]) -> bool:
    return _meaningful(document.get("deletedAt"))


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def _milliseconds_to_rfc3339(value: Any) -> str | None:
    milliseconds = _number(value)
    if milliseconds is None or milliseconds <= 0:
        return None
    try:
        completed = datetime.fromtimestamp(milliseconds / 1_000, UTC)
        # Reject corrupt timestamps well beyond any plausible local-clock skew.
        if completed > datetime.now(UTC).replace(microsecond=0) + timedelta(days=2):
            return None
        rendered = completed.isoformat(timespec="milliseconds")
    except (OSError, OverflowError, ValueError):
        return None
    return rendered.replace("+00:00", "Z")


def _sort_number(value: Any) -> float:
    number = _number(value)
    return float(number) if number is not None else math.inf


def _document_sort_key(document: dict[str, Any]) -> tuple[Any, ...]:
    item_type = _item_type(document)
    type_order = {"category": 0, "project": 1, "recurringTask": 2, "task": 3}
    title = document.get("title")
    identifier = document.get("_id")
    return (
        _sort_number(document.get("masterRank")),
        _sort_number(document.get("rank")),
        type_order.get(item_type, 9),
        title.casefold() if isinstance(title, str) else "",
        identifier if isinstance(identifier, str) else "",
    )


def _ordered_subtasks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        source = list(value.values())
    elif isinstance(value, list):
        source = value
    else:
        return []
    subtasks = [item for item in source if isinstance(item, dict)]
    subtasks.sort(
        key=lambda item: (
            _sort_number(item.get("rank")),
            str(item.get("title", "")).casefold(),
            str(item.get("_id", item.get("id", ""))),
        )
    )
    result = []
    for order, subtask in enumerate(subtasks, start=1):
        identifier = subtask.get("_id", subtask.get("id"))
        title = subtask.get("title")
        if not isinstance(identifier, str) or not isinstance(title, str):
            continue
        compact: dict[str, Any] = {
            "id": identifier,
            "title": title,
            "order": order,
            "done": subtask.get("done") is True,
        }
        completed_at = _milliseconds_to_rfc3339(subtask.get("doneAt"))
        if completed_at is not None:
            compact["completedAt"] = completed_at
        result.append(compact)
    return result


def _set_if_meaningful(target: dict[str, Any], key: str, value: Any) -> None:
    if _meaningful(value):
        target[key] = value


def _compact_document(document: dict[str, Any], *, depth: int) -> dict[str, Any]:
    item_type = _item_type(document)
    assert item_type is not None
    parent_id = document.get("parentId")
    if not isinstance(parent_id, str) or not parent_id:
        parent_id = "unassigned"
    compact: dict[str, Any] = {
        "id": document["_id"],
        "type": item_type,
        "title": document["title"],
        "parentId": parent_id,
        "depth": depth,
        "done": document.get("done") is True,
    }
    for key in ("createdAt", "updatedAt"):
        value = _number(document.get(key))
        if value is not None:
            compact[key] = value
    raw_completed_at = document.get("doneAt")
    if raw_completed_at is None:
        raw_completed_at = document.get("completedAt")
    completed_at = _milliseconds_to_rfc3339(raw_completed_at)
    if completed_at is not None:
        compact["completedAt"] = completed_at
    completed_on = document.get("doneDate")
    if isinstance(completed_on, str) and completed_on:
        compact["completedOn"] = completed_on
    deleted_at = _milliseconds_to_rfc3339(document.get("deletedAt"))
    if deleted_at is not None:
        compact["trashedAt"] = deleted_at

    scheduled_date = document.get("day")
    if isinstance(scheduled_date, str) and scheduled_date != "unassigned":
        compact["scheduledDate"] = scheduled_date
    for source_key, context_key in (
        ("dueDate", "dueDate"),
        ("startDate", "startDate"),
        ("endDate", "endDate"),
        ("plannedWeek", "plannedWeek"),
        ("plannedMonth", "plannedMonth"),
        ("note", "note"),
    ):
        _set_if_meaningful(compact, context_key, document.get(source_key))
    time_estimate = _number(document.get("timeEstimate"))
    if time_estimate is not None and time_estimate > 0:
        compact["timeEstimateMs"] = time_estimate
    label_ids = document.get("labelIds")
    if isinstance(label_ids, list) and label_ids:
        compact["labelIds"] = [value for value in label_ids if isinstance(value, str)]
    for rank_key in ("rank", "masterRank"):
        rank = _number(document.get(rank_key))
        if rank is not None:
            compact[rank_key] = rank

    subtasks = _ordered_subtasks(document.get("subtasks"))
    if subtasks:
        compact["subtasks"] = subtasks
    if item_type == "recurringTask":
        recurrence: dict[str, Any] = {
            "scope": "series",
            "cadence": document.get("type"),
        }
        _set_if_meaningful(recurrence, "startDate", document.get("repeatStart"))
        _set_if_meaningful(recurrence, "endDate", document.get("endDate"))
        compact["recurrence"] = recurrence
    elif item_type == "task" and document.get("recurring") is True:
        recurrence = {"scope": "occurrence"}
        _set_if_meaningful(recurrence, "seriesId", document.get("recurringTaskId"))
        _set_if_meaningful(recurrence, "scheduledDate", document.get("day"))
        compact["recurrence"] = recurrence
    return compact


def _container_path(
    document: dict[str, Any], documents_by_id: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, str]], bool]:
    reverse_path = []
    seen: set[str] = set()
    current: dict[str, Any] | None = document
    cyclic = False
    while current is not None:
        identifier = current.get("_id")
        item_type = _item_type(current)
        title = current.get("title")
        if not isinstance(identifier, str) or item_type not in {"category", "project"}:
            break
        if identifier in seen:
            cyclic = True
            break
        seen.add(identifier)
        reverse_path.append({"id": identifier, "type": item_type, "title": str(title or "")})
        parent_id = current.get("parentId")
        current = documents_by_id.get(parent_id) if isinstance(parent_id, str) else None
    return list(reversed(reverse_path)), cyclic


def _candidate_description(
    document: dict[str, Any], documents_by_id: dict[str, dict[str, Any]]
) -> str:
    path, _cyclic = _container_path(document, documents_by_id)
    titles = " / ".join(node["title"] for node in path)
    return f"{document['_id']} ({titles})"


def _select_root(
    query: str,
    documents_by_id: dict[str, dict[str, Any]],
    *,
    include_trash: bool,
) -> dict[str, Any]:
    containers = [
        document
        for document in documents_by_id.values()
        if _item_type(document) in {"category", "project"}
        and (include_trash or not _is_trashed(document))
    ]
    direct = documents_by_id.get(query)
    if direct is not None and direct in containers:
        return direct
    exact = [document for document in containers if document.get("title") == query]
    candidates = exact or [
        document
        for document in containers
        if isinstance(document.get("title"), str)
        and document["title"].casefold() == query.casefold()
    ]
    if not candidates:
        normalized_query = normalized_title(query)
        candidates = [
            document
            for document in containers
            if isinstance(document.get("title"), str)
            and normalized_title(document["title"]) == normalized_query
        ]
    if not candidates:
        raise PlanSemanticError(
            f"no active Marvin category/project matches title or ID {query!r} in the backup"
        )
    if len(candidates) > 1:
        descriptions = "; ".join(
            _candidate_description(document, documents_by_id)
            for document in sorted(candidates, key=_document_sort_key)
        )
        raise PlanSemanticError(
            f"Marvin category/project title {query!r} is ambiguous; rerun with its exact ID: "
            + descriptions
        )
    return candidates[0]


def build_project_context(
    documents: list[dict[str, Any]],
    query: str,
    *,
    include_trash: bool = False,
    source_format: str = "marvin-backup-json",
    source_bytes: int | None = None,
    max_depth: int | None = None,
    state: Literal["all", "open", "completed"] = "all",
    since: date | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """Build deterministic, compact context for all descendants of one category/project."""

    documents_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    malformed_supported = 0
    for document in documents:
        if document.get("db") not in SUPPORTED_DATABASES:
            continue
        item_type = _item_type(document)
        if item_type is None:
            continue
        identifier = document.get("_id")
        title = document.get("title")
        if not isinstance(identifier, str) or not identifier or not isinstance(title, str):
            malformed_supported += 1
            continue
        previous = documents_by_id.get(identifier)
        if previous is not None:
            duplicate_ids.add(identifier)
            previous_updated = _number(previous.get("updatedAt"))
            candidate_updated = _number(document.get("updatedAt"))
            previous_order = float(previous_updated) if previous_updated is not None else -math.inf
            candidate_order = (
                float(candidate_updated) if candidate_updated is not None else -math.inf
            )
            if candidate_order <= previous_order:
                continue
        documents_by_id[identifier] = document

    root = _select_root(query, documents_by_id, include_trash=include_trash)
    root_id = root["_id"]
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for document in documents_by_id.values():
        parent_id = document.get("parentId")
        if isinstance(parent_id, str):
            children[parent_id].append(document)
    for values in children.values():
        values.sort(key=_document_sort_key)

    compact_items: list[dict[str, Any]] = []
    visited = {root_id}
    cycles: set[str] = set()
    excluded_trash = 0

    def visit(parent_id: str, depth: int, *, excluded_ancestor: bool = False) -> None:
        nonlocal excluded_trash
        for child in children.get(parent_id, []):
            child_id = child["_id"]
            if child_id in visited:
                cycles.add(child_id)
                continue
            visited.add(child_id)
            excluded = excluded_ancestor or (not include_trash and _is_trashed(child))
            if excluded:
                excluded_trash += 1
            else:
                compact_items.append(_compact_document(child, depth=depth))
            visit(child_id, depth + 1, excluded_ancestor=excluded)

    visit(root_id, 1)

    if max_depth is not None:
        compact_items = [item for item in compact_items if item["depth"] <= max_depth]
    if state == "open":
        compact_items = [item for item in compact_items if not item["done"]]
    elif state == "completed":
        compact_items = [item for item in compact_items if item["done"]]
    if since is not None:
        since_text = since.isoformat()
        retained = []
        for item in compact_items:
            completion = item.get("completedOn", str(item.get("completedAt", ""))[:10])
            if not item["done"] or not completion or completion >= since_text:
                retained.append(item)
        compact_items = retained

    root_path, root_path_cyclic = _container_path(root, documents_by_id)
    if root_path_cyclic:
        cycles.add(root_id)
    warnings = []
    if duplicate_ids:
        warnings.append(
            f"Resolved {len(duplicate_ids)} duplicate document ID(s) by latest updatedAt."
        )
    if malformed_supported:
        warnings.append(
            f"Ignored {malformed_supported} supported document(s) missing a valid ID/title."
        )
    if cycles:
        warnings.append(f"Stopped at {len(cycles)} hierarchy cycle(s).")
    missing_completion = sum(
        item["done"] and "completedAt" not in item and "completedOn" not in item
        for item in compact_items
    )
    if missing_completion:
        warnings.append(
            f"{missing_completion} completed descendant(s) have no completion timestamp."
        )

    type_counts = {
        item_type: sum(item["type"] == item_type for item in compact_items)
        for item_type in ("category", "project", "task", "recurringTask")
    }
    root_compact = _compact_document(root, depth=0)
    root_compact.pop("parentId", None)
    root_compact.pop("depth", None)
    root_compact["path"] = root_path
    source: dict[str, Any] = {
        "kind": "marvin-backup",
        "format": source_format,
        "documentCount": len(documents),
        "supportedDocumentCount": len(documents_by_id),
    }
    if source_bytes is not None:
        source["inputBytes"] = source_bytes
    result = {
        "contextVersion": 1,
        "kind": "project",
        "source": source,
        "root": root_compact,
        "counts": {
            "descendants": len(compact_items),
            **type_counts,
            "open": sum(not item["done"] for item in compact_items),
            "completed": sum(item["done"] for item in compact_items),
            "withSubtasks": sum(bool(item.get("subtasks")) for item in compact_items),
            "trashedIncluded": sum("trashedAt" in item for item in compact_items),
            "trashedExcluded": excluded_trash,
        },
        "items": compact_items,
        "warnings": warnings,
    }
    if summary:
        completed_dates = sorted(
            (
                item.get("completedOn", str(item.get("completedAt", ""))[:10])
                for item in compact_items
                if item["done"]
            ),
            reverse=True,
        )
        result["summary"] = {
            "directChildren": [
                {key: item[key] for key in ("id", "type", "title", "done") if key in item}
                for item in compact_items
                if item["depth"] == 1
            ],
            "latestCompletion": completed_dates[0] if completed_dates else None,
            "oldestRetainedCompletion": completed_dates[-1] if completed_dates else None,
            "recurrenceDefinitions": sum(item["type"] == "recurringTask" for item in compact_items),
            "generatedOccurrences": sum(
                item.get("recurrence", {}).get("scope") == "occurrence" for item in compact_items
            ),
        }
        result.pop("items")
    return result


def project_context_json(
    backup_path: Path,
    query: str,
    *,
    include_trash: bool = False,
    max_depth: int | None = None,
    state: Literal["all", "open", "completed"] = "all",
    since: date | None = None,
    summary: bool = False,
) -> str:
    from marvin_pilot.backup_cache import load_cached_backup_documents

    documents, source_format, source_bytes, digest, cache_hit = load_cached_backup_documents(
        backup_path
    )
    context = build_project_context(
        documents,
        query,
        include_trash=include_trash,
        source_format=source_format,
        source_bytes=source_bytes,
        max_depth=max_depth,
        state=state,
        since=since,
        summary=summary,
    )
    context["source"]["sha256"] = digest
    context["source"]["cacheHit"] = cache_hit
    return json.dumps(context, ensure_ascii=False, indent=2) + "\n"


def normalized_title(value: str) -> str:
    """Normalize display decoration for candidate ranking, never identity selection."""

    value = unicodedata.normalize("NFKC", value)
    value = "".join(
        character
        for character in value
        if unicodedata.category(character) != "Cf" and character != "\ufe0f"
    )
    value = re.sub(r"^\W+", "", value, flags=re.UNICODE)
    return " ".join(value.casefold().split())


def search_backup_context(documents: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """Return ranked title/path candidates without silently choosing an ambiguous match."""

    documents_by_id = {
        document["_id"]: document
        for document in documents
        if _item_type(document) is not None and isinstance(document.get("_id"), str)
    }
    normalized_query = normalized_title(query)
    candidates = []
    for document in documents_by_id.values():
        title = document.get("title")
        if not isinstance(title, str) or _is_trashed(document):
            continue
        normalized = normalized_title(title)
        if title == query:
            reason, score = "exact title", 0
        elif normalized == normalized_query:
            reason, score = "normalized title", 1
        elif normalized_query and normalized_query in normalized:
            reason, score = "normalized substring", 2
        else:
            continue
        path = []
        if _item_type(document) in {"category", "project"}:
            path, _cyclic = _container_path(document, documents_by_id)
        else:
            parent = documents_by_id.get(document.get("parentId"))
            if parent is not None:
                path, _cyclic = _container_path(parent, documents_by_id)
        candidates.append(
            {
                "id": document["_id"],
                "type": _item_type(document),
                "title": title,
                "match": reason,
                "path": path,
                "_score": score,
            }
        )
    candidates.sort(key=lambda item: (item["_score"], item["title"].casefold(), item["id"]))
    for item in candidates:
        item.pop("_score")
    return {
        "contextVersion": 1,
        "kind": "search",
        "query": query,
        "normalizedQuery": normalized_query,
        "count": len(candidates),
        "ambiguous": len(candidates) > 1,
        "candidates": candidates[:100],
    }


def scheduled_day_context(documents: list[dict[str, Any]], scheduled_date: date) -> dict[str, Any]:
    """Return exact backup documents scheduled for a date; never claim live Today semantics."""

    items = []
    for document in documents:
        if _item_type(document) != "task" or document.get("day") != scheduled_date.isoformat():
            continue
        if _is_trashed(document):
            continue
        compact = _compact_document(document, depth=0)
        occurrence = document.get("recurring") is True
        compact["scheduleKind"] = "generated-occurrence" if occurrence else "ordinary-task"
        if occurrence:
            identifier = str(document.get("_id", ""))
            compact["rolledOver"] = scheduled_date.isoformat() not in identifier
        items.append(compact)
    items.sort(key=lambda item: (_sort_number(item.get("rank")), item["title"].casefold()))
    return {
        "contextVersion": 1,
        "kind": "scheduled-day",
        "date": scheduled_date.isoformat(),
        "accuracyNote": (
            "Backup snapshot only: exact day fields are shown, but this is not a claim about "
            "Marvin's current Today strategy, rollover, or UI cache."
        ),
        "count": len(items),
        "items": items,
    }
