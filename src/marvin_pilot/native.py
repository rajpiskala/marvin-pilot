"""Paired, loopback-only transport for a supported, already-open Marvin tab."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import keyring

from marvin_pilot.database import (
    MAX_DATABASE_RESPONSE_BYTES,
    CouchDatabase,
    DatabaseMarvinClient,
)
from marvin_pilot.errors import (
    AmbiguousMutationError,
    CredentialError,
    LivePreconditionError,
    PlanSemanticError,
    RemoteError,
)
from marvin_pilot.preflight import coupled_task_reasons

NATIVE_PORT = 48631
NATIVE_PROTOCOL = 1
KEYRING_SERVICE = "marvin-pilot-native"
KEYRING_USER = "loopback-pairing"
NATIVE_FIELDS = {"title", "note", "estimatedTimeDuration"}
API_HOST = "https://serv.amazingmarvin.com"


def pair_native(*, backend=keyring):
    secret = secrets.token_urlsafe(32)
    try:
        backend.set_password(KEYRING_SERVICE, KEYRING_USER, secret)
    except Exception:
        raise CredentialError("could not save the native pairing code in the OS keyring") from None
    return secret


def native_secret(*, backend=keyring):
    try:
        secret = backend.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        raise CredentialError(
            "could not read the native pairing code from the OS keyring"
        ) from None
    if not secret:
        raise CredentialError("run 'marvin-pilot native pair' and pair the userscript first")
    return secret


def check_native_plan(plan):
    if plan.expectedAccount is None:
        raise PlanSemanticError("native apply requires an exact expectedAccount pin")
    for operation in plan.operations:
        after = getattr(operation, "after", None)
        fields = after.model_fields_set if after is not None else set()
        permitted = NATIVE_FIELDS | ({"parent"} if operation.action == "create" else set())
        if (
            operation.target.type != "task"
            or getattr(operation.target, "recurrence", None) is not None
            or operation.action not in {"create", "update", "trash"}
            or fields - permitted
            or getattr(operation, "completedAt", None) is not None
            or getattr(operation, "siblingOrder", None) is not None
        ):
            raise PlanSemanticError(
                f"operation {operation.operationId!r} is not supported by native mode: "
                "only ordinary open tasks, title/note/estimate edits and plain creates/Trash"
            )


def check_native_task(document):
    if document is not None and (
        document.get("db") != "Tasks"
        or document.get("done")
        or coupled_task_reasons(document)
        or any(
            document.get(name)
            for name in (
                "recurringTaskId",
                "habitId",
                "savedItemId",
                "reminders",
                "attachments",
                "_attachments",
                "shareId",
                "dependencies",
                "dependedOnBy",
                "_conflicts",
            )
        )
    ):
        raise LivePreconditionError(
            "native mode requires an ordinary open task without linked data"
        )


@dataclass(frozen=True)
class NativeAccount:
    account_user_id: str
    account_email: str


class NativeBridge:
    """Single client, one command at a time, with no replay of a claimed command."""

    api_base_host = API_HOST
    api_base_url = API_HOST + "/api"

    def __init__(self, account, secret, *, port=NATIVE_PORT, timeout=35.0, long_poll=1.0):
        self.account = account
        self.timeout = timeout
        self.long_poll = long_poll
        self._secret = ("Bearer " + secret).encode("utf-8")
        self._condition = threading.Condition()
        self._tab = None
        self._pending = None
        self._closed = False
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        except OSError:
            raise RemoteError("native loopback port is in use or unavailable") from None
        self.host = f"127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def call(self, method, data, *, mutation=False):
        expires = time.monotonic() + self.timeout
        pending = {
            "id": secrets.token_urlsafe(20),
            "method": method,
            "data": data,
            "expiresAt": int((time.time() + self.timeout) * 1000),
        }
        with self._condition:
            if self._pending is not None or self._closed:
                raise RemoteError("native bridge is busy or closed")
            self._pending = pending
            self._condition.notify_all()
            try:
                while "reply" not in pending and not self._closed:
                    remaining = expires - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                reply = pending.get("reply")
                if reply is not None and reply.get("ok") is True:
                    return reply.get("data")
                if mutation and pending.get("claimed"):
                    raise AmbiguousMutationError(
                        "native write was delivered but not confirmed; reconcile from the receipt"
                    )
                if reply is not None:
                    if method == "ping":
                        raise RemoteError(
                            "the paired Marvin tab's local database did not respond; "
                            "refresh that tab and wait for it to finish loading"
                        )
                    raise RemoteError(
                        "Marvin tab refused the native command or could not verify it"
                    )
                raise RemoteError("no reply from a paired, ready Marvin tab for the plan's account")
            finally:
                self._pending = None
                self._condition.notify_all()

    def check_connection(self):
        if self.call("ping", {}) != {"ready": True}:
            raise CredentialError("native tab could not prove that it is ready")
        return self.account

    def dispatch(self, path, message):
        tab = message.get("tab")
        user_id, email = message.get("userId"), message.get("email")
        if (
            not isinstance(tab, str)
            or not (20 <= len(tab) <= 80)
            or message.get("protocol") != NATIVE_PROTOCOL
            or user_id != self.account.account_user_id
            or not isinstance(email, str)
            or email.casefold() != self.account.account_email.casefold()
        ):
            return 409, {}
        with self._condition:
            if self._tab is not None and tab != self._tab:
                return 409, {}
            if path == "/next":
                until = time.monotonic() + self.long_poll
                while not self._closed:
                    pending = self._pending
                    if pending and not pending.get("claimed"):
                        self._tab = tab
                        pending["claimed"] = True
                        return 200, {
                            "command": {
                                k: pending[k] for k in ("id", "method", "data", "expiresAt")
                            }
                        }
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                return 200, {"command": None}
            pending = self._pending
            if (
                not pending
                or not pending.get("claimed")
                or "reply" in pending
                or message.get("id") != pending["id"]
                or tab != self._tab
                or type(message.get("ok")) is not bool
            ):
                return 409, {}
            pending["reply"] = {"ok": message["ok"], "data": message.get("data")}
            self._condition.notify_all()
            return 200, {}

    def _handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.connection.settimeout(6)
                auth = self.headers.get("Authorization", "").encode("utf-8")
                if (
                    self.path not in {"/next", "/reply"}
                    or self.headers.get("Host") != bridge.host
                    or not hmac.compare_digest(auth, bridge._secret)
                ):
                    self._send(403, {})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if self.headers.get("Transfer-Encoding") or not (
                        0 < length <= MAX_DATABASE_RESPONSE_BYTES
                    ):
                        raise ValueError
                    message = json.loads(self.rfile.read(length))
                    if not isinstance(message, dict):
                        raise ValueError
                except (ValueError, OSError):
                    self._send(400, {})
                    return
                self._send(*bridge.dispatch(self.path, message))

            def _send(self, status, value):
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with suppress(OSError):
                    self.wfile.write(body)

        return Handler


class NativeDatabase(CouchDatabase):
    """Use the normal row and per-document result validation over native RPC."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.credentials = bridge.account
        self.deleted_revisions = {}

    def close(self):
        self.bridge.close()

    def request(self, method, path="", *, body=None, params=None, mutation=False):
        if method != "POST" or path not in {"/_all_docs", "/_bulk_docs"}:
            raise LivePreconditionError("this read or write is not supported by native mode")
        kind = "read" if path == "/_all_docs" else "write"
        return self.bridge.call(kind, body, mutation=mutation)


class NativeMarvinClient(DatabaseMarvinClient):
    experimental_native = True

    def __init__(self, bridge):
        super().__init__(bridge, NativeDatabase(bridge))

    def close(self):
        self.database.close()

    def prefetch_operations(self, operations):
        super().prefetch_operations(operations)
        for operation in operations:
            check_native_task(self._read_cache.get(operation.target.id))

    def mutation_document(self, compiled, current):
        check_native_task(current)
        projected = super().mutation_document(compiled, current)
        if not projected.get("_deleted"):
            check_native_task(projected)
        return projected


def connect_native(user_id, email):
    if not user_id or not email:
        raise PlanSemanticError("native mode requires a pinned Marvin account")
    return NativeMarvinClient(NativeBridge(NativeAccount(user_id, email), native_secret()))
