"""Loopback visualizer; mutation is opt-in and bound to a reviewed local file."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote

from marvin_pilot.errors import MarvinPilotError, PlanSemanticError
from marvin_pilot.models.plan_v1 import ChangePlanV1
from marvin_pilot.plan_io import MAX_PLAN_BYTES, parse_plan_bytes
from marvin_pilot.visualizer import build_plan_view
from marvin_pilot.visualizer_hierarchy import HierarchyContext

LOOPBACK_HOST = "127.0.0.1"
MAX_FILENAME_LENGTH = 255
ASSET_PACKAGE = "marvin_pilot.visualizer_assets"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; font-src 'none'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _safe_source_name(value: str | None, *, url_encoded: bool = False) -> str | None:
    if value is None:
        return None
    if url_encoded:
        value = unquote(value, errors="replace")
    name = value.replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()
    if not name:
        return None
    return name[:MAX_FILENAME_LENGTH]


def _asset_bytes(name: str) -> bytes:
    return files(ASSET_PACKAGE).joinpath(name).read_bytes()


class _LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


@dataclass(frozen=True, slots=True)
class LoadedVisualization:
    plan: ChangePlanV1 | None
    source_name: str | None = None
    hierarchy: HierarchyContext | None = None
    hierarchy_sources: tuple[str, ...] = ()
    review_state: Literal["preview", "applied"] = "preview"
    receipt_id: str | None = None
    raw: bytes | None = None
    extra_watch_paths: tuple[Path, ...] = ()


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class VisualizerServer:
    """One loopback visualizer with optional watched inputs and reviewed apply."""

    def __init__(
        self,
        *,
        preloaded_plan: ChangePlanV1 | None = None,
        source_name: str | None = None,
        session_token: str | None = None,
        hierarchy: HierarchyContext | None = None,
        hierarchy_sources: tuple[str, ...] = (),
        review_state: Literal["preview", "applied"] = "preview",
        receipt_id: str | None = None,
        preloaded_raw: bytes | None = None,
        plan_path: Path | None = None,
        watch_paths: tuple[Path, ...] = (),
        reload_visualization: Callable[[], LoadedVisualization] | None = None,
        review_preflight: Callable[[ChangePlanV1], dict[str, Any]] | None = None,
        review_apply: Callable[
            [ChangePlanV1, bytes, Callable[[], None], dict[str, Any]], dict[str, Any]
        ]
        | None = None,
        session_key: str | None = None,
    ) -> None:
        self.session_token = session_token or secrets.token_urlsafe(32)
        self.session_key = session_key
        self.csrf_token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._stop_watch = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watch_paths = watch_paths
        self._watch_signatures = tuple(_file_signature(path) for path in watch_paths)
        self._reload_visualization = reload_visualization
        self._reload_error: str | None = None
        self._revision = 1 if preloaded_plan is not None else 0
        self._plan = preloaded_plan
        self._plan_path = plan_path.resolve() if plan_path is not None else None
        self._source_hash = hashlib.sha256(preloaded_raw).hexdigest() if preloaded_raw else None
        self._review_preflight = review_preflight
        self._review_apply = review_apply
        self._review_challenge: tuple[str, str, int, float, dict[str, Any]] | None = None
        self._apply_busy = False
        self._hierarchy = hierarchy
        self._hierarchy_sources = hierarchy_sources
        projection_started = time.perf_counter()
        self._current = (
            build_plan_view(
                preloaded_plan,
                source_name=_safe_source_name(source_name),
                hierarchy=self._hierarchy,
                hierarchy_sources=hierarchy_sources,
                review_state=review_state,
                receipt_id=receipt_id,
            ).to_dict()
            if preloaded_plan is not None
            else None
        )
        self.projection_ms = (time.perf_counter() - projection_started) * 1000
        bind_started = time.perf_counter()
        self._httpd = _LoopbackHTTPServer((LOOPBACK_HOST, 0), self._handler_type())
        self.bind_ms = (time.perf_counter() - bind_started) * 1000
        self._serving = False
        self.port = int(self._httpd.server_address[1])
        self.expected_host = f"{LOOPBACK_HOST}:{self.port}"
        self.origin = f"http://{self.expected_host}"
        self.url = f"{self.origin}/{self.session_token}/"

    def _can_apply(self) -> bool:
        return (
            self._plan_path is not None
            and self._plan is not None
            and self._plan.expectedAccount is not None
            and self._source_hash is not None
            and self._review_preflight is not None
            and self._review_apply is not None
            and self._current is not None
            and self._current["review_state"] == "preview"
            and self._reload_error is None
        )

    def _status(self) -> dict[str, Any]:
        with self._lock:
            account = self._plan.expectedAccount if self._plan is not None else None
            return {
                "revision": self._revision,
                "error": self._reload_error,
                "can_apply": self._can_apply(),
                "apply_busy": self._apply_busy,
                "source_path": str(self._plan_path) if self._plan_path else None,
                "account": (
                    {"email": account.email, "user_id": account.userId}
                    if account is not None
                    else None
                ),
                "csrf": self.csrf_token,
                "session_key": self.session_key,
            }

    def _assert_reviewed_source(self, identity: dict[str, Any]) -> tuple[ChangePlanV1, bytes]:
        with self._lock:
            if not self._can_apply() or self._plan_path is None or self._plan is None:
                raise PlanSemanticError("this view is not eligible for browser apply")
            if (
                identity.get("revision") != self._revision
                or identity.get("plan_id") != self._plan.planId
            ):
                raise PlanSemanticError("reviewed plan changed; reload and review it again")
            if identity.get("digest") != self._current["digest"]:
                raise PlanSemanticError("reviewed plan digest changed; reload and review it again")
            path = self._plan_path
            expected_hash = self._source_hash
            plan = self._plan
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise PlanSemanticError(f"reviewed plan file is unavailable: {exc}") from exc
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise PlanSemanticError("plan file changed after review; reload before applying")
        return plan, raw

    def _watch_inputs(self) -> None:
        while not self._stop_watch.wait(0.75):
            signatures = tuple(_file_signature(path) for path in self._watch_paths)
            if signatures == self._watch_signatures:
                continue
            self._watch_signatures = signatures
            if self._reload_visualization is None:
                continue
            try:
                loaded = self._reload_visualization()
                current = (
                    build_plan_view(
                        loaded.plan,
                        source_name=_safe_source_name(loaded.source_name),
                        hierarchy=loaded.hierarchy,
                        hierarchy_sources=loaded.hierarchy_sources,
                        review_state=loaded.review_state,
                        receipt_id=loaded.receipt_id,
                    ).to_dict()
                    if loaded.plan is not None
                    else None
                )
            except (MarvinPilotError, OSError, ValueError) as exc:
                with self._lock:
                    self._reload_error = str(exc)
                    self._review_challenge = None
                    self._revision += 1
                continue
            except Exception:
                with self._lock:
                    self._reload_error = "Unexpected local reload error; check the input files"
                    self._review_challenge = None
                    self._revision += 1
                continue
            with self._lock:
                if (
                    current is not None
                    and self._current is not None
                    and self._current["review_state"] == "applied"
                    and current["digest"] == self._current["digest"]
                ):
                    current["review_state"] = "applied"
                    current["receipt_id"] = self._current["receipt_id"]
                self._plan = loaded.plan
                self._current = current
                self._hierarchy = loaded.hierarchy
                self._hierarchy_sources = loaded.hierarchy_sources
                if loaded.extra_watch_paths:
                    self._watch_paths = tuple(
                        dict.fromkeys((*self._watch_paths, *loaded.extra_watch_paths))
                    )
                    self._watch_signatures = tuple(
                        _file_signature(path) for path in self._watch_paths
                    )
                self._source_hash = hashlib.sha256(loaded.raw).hexdigest() if loaded.raw else None
                self._reload_error = None
                self._review_challenge = None
                self._revision += 1

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class VisualizerHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "MarvinPilotVisualizer"
            sys_version = ""

            def log_message(self, _format: str, *args: object) -> None:
                return

            def _headers_allowed(self) -> bool:
                if self.headers.get("Host") != owner.expected_host:
                    self._send_json(
                        HTTPStatus.MISDIRECTED_REQUEST,
                        {"error": {"message": "unexpected Host header"}},
                    )
                    return False
                origin = self.headers.get("Origin")
                if origin is not None and origin != owner.origin:
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {"error": {"message": "unexpected Origin header"}},
                    )
                    return False
                return True

            def _send_headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                if self.close_connection:
                    self.send_header("Connection", "close")
                self.end_headers()

            def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
                self._send_headers(status, content_type, len(body))
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _send_json(self, status: HTTPStatus, value: Any) -> None:
                body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                self._send_bytes(status, "application/json; charset=utf-8", body)

            def _route(self) -> str | None:
                path = self.path.split("?", maxsplit=1)[0]
                prefix = f"/{owner.session_token}/"
                if not path.startswith(prefix):
                    return None
                return path[len(prefix) :]

            def _discard_small_request_body(self) -> None:
                """Avoid a TCP reset after rejecting a small JSON POST on Windows."""

                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return
                if 0 < length <= 4096:
                    self.rfile.read(length)

            def do_GET(self) -> None:
                if not self._headers_allowed():
                    return
                route = self._route()
                if route is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
                elif route == "":
                    self._send_bytes(
                        HTTPStatus.OK,
                        "text/html; charset=utf-8",
                        _asset_bytes("index.html"),
                    )
                elif route == "app.js":
                    self._send_bytes(
                        HTTPStatus.OK,
                        "text/javascript; charset=utf-8",
                        _asset_bytes("app.js"),
                    )
                elif route == "styles.css":
                    self._send_bytes(
                        HTTPStatus.OK,
                        "text/css; charset=utf-8",
                        _asset_bytes("styles.css"),
                    )
                elif route == "marvin-pilot.png":
                    self._send_bytes(
                        HTTPStatus.OK,
                        "image/png",
                        _asset_bytes("marvin-pilot.png"),
                    )
                elif route == "favicon-64.png":
                    self._send_bytes(
                        HTTPStatus.OK,
                        "image/png",
                        _asset_bytes("favicon-64.png"),
                    )
                elif route == "api/current":
                    with owner._lock:
                        self._send_json(HTTPStatus.OK, {"plan": owner._current, **owner._status()})
                elif route == "api/status":
                    self._send_json(HTTPStatus.OK, owner._status())
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                self.close_connection = True
                if not self._headers_allowed():
                    self._discard_small_request_body()
                    return
                route = self._route()
                if route in ("api/preflight", "api/apply"):
                    self._review_action(route)
                    return
                if route != "api/plan":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._send_json(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        {"error": {"message": "Content-Type must be application/json"}},
                    )
                    return
                if self.headers.get("Transfer-Encoding") is not None:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": {"message": "streamed request bodies are unsupported"}},
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    length = -1
                if length < 0:
                    self._send_json(
                        HTTPStatus.LENGTH_REQUIRED,
                        {"error": {"message": "a valid Content-Length is required"}},
                    )
                    return
                if length > MAX_PLAN_BYTES:
                    self._send_json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": {"message": f"plan exceeds the {MAX_PLAN_BYTES}-byte limit"}},
                    )
                    return
                raw = self.rfile.read(length)
                try:
                    plan = parse_plan_bytes(raw)
                    source_name = _safe_source_name(
                        self.headers.get("X-Marvin-Pilot-Filename"), url_encoded=True
                    )
                    current = build_plan_view(
                        plan,
                        source_name=source_name,
                        hierarchy=owner._hierarchy,
                        hierarchy_sources=owner._hierarchy_sources,
                    ).to_dict()
                    with owner._lock:
                        owner._current = current
                        owner._plan = plan
                        owner._plan_path = None
                        owner.session_key = None
                        owner._source_hash = None
                        owner._reload_visualization = None
                        owner._review_challenge = None
                        owner._revision += 1
                except MarvinPilotError as exc:
                    self._send_json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": {"code": exc.exit_code, "message": str(exc)}},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"plan": owner._current, **owner._status()})

            def _review_action(self, route: str) -> None:
                if (
                    self.headers.get("Origin") != owner.origin
                    or self.headers.get("X-Marvin-Pilot-CSRF") != owner.csrf_token
                    or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
                ):
                    self._discard_small_request_body()
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {"error": {"message": "review origin or CSRF check failed"}},
                    )
                    return
                if self.headers.get_content_type() != "application/json" or self.headers.get(
                    "Transfer-Encoding"
                ):
                    self._send_json(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        {"error": {"message": "JSON request required"}},
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    length = -1
                if not 0 < length <= 4096:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": {"message": "invalid review request size"}},
                    )
                    return
                try:
                    request = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    request = None
                if not isinstance(request, dict) or not isinstance(request.get("identity"), dict):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": {"message": "invalid review identity"}}
                    )
                    return
                identity = request["identity"]
                try:
                    if route == "api/preflight":
                        with owner._lock:
                            if owner._apply_busy:
                                raise PlanSemanticError("another apply is already in progress")
                        plan, _raw = owner._assert_reviewed_source(identity)
                        assert owner._review_preflight is not None
                        result = owner._review_preflight(plan)
                        owner._assert_reviewed_source(identity)
                        challenge = secrets.token_urlsafe(32)
                        with owner._lock:
                            owner._review_challenge = (
                                challenge,
                                owner._source_hash or "",
                                owner._revision,
                                time.monotonic() + 300,
                                result,
                            )
                        self._send_json(
                            HTTPStatus.OK, {"challenge": challenge, "preflight": result}
                        )
                        return
                    with owner._lock:
                        issued = owner._review_challenge
                        owner._review_challenge = None
                        if owner._apply_busy:
                            raise PlanSemanticError("another apply is already in progress")
                        if (
                            issued is None
                            or request.get("challenge") != issued[0]
                            or owner._source_hash != issued[1]
                            or owner._revision != issued[2]
                            or time.monotonic() >= issued[3]
                        ):
                            raise PlanSemanticError(
                                "preflight approval expired; review and preflight again"
                            )
                        owner._apply_busy = True
                    try:
                        plan, raw = owner._assert_reviewed_source(identity)
                        assert owner._review_apply is not None
                        result = owner._review_apply(
                            plan, raw, lambda: owner._assert_reviewed_source(identity), issued[4]
                        )
                        with owner._lock:
                            if owner._current is not None:
                                owner._current["review_state"] = "applied"
                                owner._current["receipt_id"] = result.get("receipt_id")
                            owner._revision += 1
                    finally:
                        with owner._lock:
                            owner._apply_busy = False
                    self._send_json(HTTPStatus.OK, {"result": result, **owner._status()})
                except MarvinPilotError as exc:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"error": {"code": exc.exit_code, "message": str(exc)}},
                    )
                except Exception:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "error": {
                                "message": "Unexpected error. Check Pilot history before retrying."
                            }
                        },
                    )

            def _method_not_allowed(self) -> None:
                if not self._headers_allowed():
                    return
                self._send_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": {"message": "method not allowed"}},
                )

            do_DELETE = _method_not_allowed
            do_HEAD = _method_not_allowed
            do_OPTIONS = _method_not_allowed
            do_PATCH = _method_not_allowed
            do_PUT = _method_not_allowed

        return VisualizerHandler

    def serve_forever(self) -> None:
        """Serve until Ctrl+C or an explicit shutdown."""

        self._serving = True
        if self._watch_paths and self._reload_visualization is not None:
            self._watch_thread = threading.Thread(target=self._watch_inputs, daemon=True)
            self._watch_thread.start()
        try:
            self._httpd.serve_forever(poll_interval=0.1)
        finally:
            self._serving = False
            self._stop_watch.set()
            if self._watch_thread is not None:
                self._watch_thread.join(timeout=2)

    def shutdown(self) -> None:
        """Stop serving and release the loopback port."""

        if self._serving:
            self._httpd.shutdown()
        self._httpd.server_close()
