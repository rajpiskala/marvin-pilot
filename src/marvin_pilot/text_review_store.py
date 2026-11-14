"""Durable field-level feedback merge, shared by tabs and local visualizer processes."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

from marvin_pilot.atomic import atomic_write_bytes
from marvin_pilot.errors import PlanSemanticError
from marvin_pilot.models.plan_v1 import ChangePlanV1
from marvin_pilot.plan_io import MAX_PLAN_BYTES, plan_digest
from marvin_pilot.text_review import build_text_review


def edit_key(edit: dict[str, Any]) -> tuple[str, str, str | None]:
    return edit["operationId"], edit["field"], edit.get("subtaskId")


@contextmanager
def review_lock(path: Path, *, timeout: float = 5):
    """Serialize read/merge/replace across processes without leaving a stale lock on exit."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise PlanSemanticError("feedback is busy; autosave will retry") from exc
                time.sleep(0.025)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)


class TextReviewStore:
    def __init__(self, source: Path, plan: ChangePlanV1) -> None:
        self.plan = plan
        self.digest = plan_digest(plan)
        identifier = self.digest.removeprefix("sha256:")
        self.path = source.with_name(f"{source.stem[:70]}.text-review-{identifier}.json")
        self.lock_path = self.path.with_suffix(".lock")
        self._mutex = threading.RLock()
        self._signature: tuple[int, int] | None = None
        self._cached: dict[str, Any] | None = None

    def _empty(self) -> dict[str, Any]:
        return {
            "reviewVersion": 1,
            "autosaveVersion": 1,
            "kind": "marvin-pilot-text-review",
            "sourcePlanId": self.plan.planId,
            "sourceDigest": self.digest,
            "requiresAIReconciliation": False,
            "version": 0,
            "edits": [],
            "clients": {},
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            if self.path.stat().st_size > MAX_PLAN_BYTES:
                raise ValueError("feedback file exceeds size limit")
            value = json.loads(self.path.read_bytes())
            if (
                not isinstance(value, dict)
                or value.get("autosaveVersion") != 1
                or value.get("kind") != "marvin-pilot-text-review"
                or value.get("sourcePlanId") != self.plan.planId
                or value.get("sourceDigest") != self.digest
                or type(value.get("version")) is not int
                or value["version"] < 1
                or not isinstance(value.get("clients"), dict)
                or not isinstance(value.get("edits"), list)
            ):
                raise ValueError("feedback identity or version is invalid")
            if value["edits"]:
                checked = build_text_review(
                    self.plan,
                    [
                        {
                            key: edit[key]
                            for key in ("operationId", "field", "subtaskId", "text")
                            if key in edit
                        }
                        for edit in value["edits"]
                    ],
                    context=value["referenceContext"],
                )
                for saved, expected in zip(value["edits"], checked["edits"], strict=True):
                    if any(saved.get(key) != item for key, item in expected.items()):
                        raise ValueError("feedback field identity changed")
                    build_text_review(
                        self.plan,
                        [
                            {
                                key: saved[key]
                                for key in ("operationId", "field", "subtaskId", "text")
                                if key in saved
                            }
                        ],
                        context=saved["referenceContext"],
                    )
            for client, state in value["clients"].items():
                UUID(client)
                if (
                    not isinstance(state, dict)
                    or type(state.get("sequence")) is not int
                    or state["sequence"] < 1
                    or not isinstance(state.get("fingerprint"), str)
                ):
                    raise ValueError("invalid autosave client sequence")
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PlanSemanticError(f"cannot read saved text feedback: {exc}") from exc

    def snapshot(self) -> dict[str, Any]:
        with self._mutex:
            try:
                stat = self.path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                signature = None
            if self._cached is None or signature != self._signature:
                self._cached = self._read()
                self._signature = signature
            return deepcopy(self._cached)

    def patch(
        self, edits: list[dict[str, Any]], *, context: dict[str, Any], client_id: str, sequence: int
    ) -> dict[str, Any]:
        try:
            UUID(client_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise PlanSemanticError("autosave requires a client UUID") from exc
        if type(sequence) is not int or not 1 <= sequence <= 2**53 - 1:
            raise PlanSemanticError("autosave requires a positive integer sequence")
        checked = build_text_review(self.plan, edits, context=context, allow_original=True)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"edits": edits, "context": context}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        with self._mutex, review_lock(self.lock_path):
            value = self._read()
            previous = value["clients"].get(client_id)
            if previous and sequence <= previous["sequence"]:
                if sequence == previous["sequence"] and fingerprint != previous["fingerprint"]:
                    raise PlanSemanticError("autosave sequence was reused with different edits")
                return value  # An old retry must not undo a newer writer.
            if client_id not in value["clients"] and len(value["clients"]) >= 4096:
                raise PlanSemanticError("too many autosave clients for this review")
            merged = {edit_key(edit): edit for edit in value["edits"]}
            for edit in checked["edits"]:
                key = edit_key(edit)
                if edit["text"] == edit["originalText"]:
                    merged.pop(key, None)
                else:
                    merged[key] = {
                        **edit,
                        "referenceContext": context,
                        "editedAt": checked["reviewedAt"],
                        "writerId": client_id,
                        "sequence": sequence,
                    }
            value.update(
                {
                    "reviewId": value.get("reviewId", checked["reviewId"]),
                    "reviewedAt": checked["reviewedAt"],
                    "referenceContext": context,
                    "requiresAIReconciliation": bool(merged),
                    "version": value["version"] + 1,
                    "edits": list(merged.values()),
                }
            )
            value["clients"][client_id] = {"sequence": sequence, "fingerprint": fingerprint}
            content = json.dumps(value, ensure_ascii=False, indent=2).encode() + b"\n"
            if len(content) > MAX_PLAN_BYTES:
                raise PlanSemanticError(
                    "saved feedback exceeds size limit; nothing was overwritten"
                )
            atomic_write_bytes(self.path, content)
            self._cached = None
            return value
