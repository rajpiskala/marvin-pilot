"""Credential-free loopback server for the read-only plan visualizer."""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import unquote

from marvin_pilot.errors import MarvinPilotError
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


class VisualizerServer:
    """One ephemeral, session-scoped visualizer server."""

    def __init__(
        self,
        *,
        preloaded_plan: ChangePlanV1 | None = None,
        source_name: str | None = None,
        session_token: str | None = None,
        hierarchy: HierarchyContext | None = None,
    ) -> None:
        self.session_token = session_token or secrets.token_urlsafe(32)
        self._hierarchy = hierarchy
        self._current = (
            build_plan_view(
                preloaded_plan,
                source_name=_safe_source_name(source_name),
                hierarchy=self._hierarchy,
            ).to_dict()
            if preloaded_plan is not None
            else None
        )
        self._httpd = _LoopbackHTTPServer((LOOPBACK_HOST, 0), self._handler_type())
        self._serving = False
        self.port = int(self._httpd.server_address[1])
        self.expected_host = f"{LOOPBACK_HOST}:{self.port}"
        self.origin = f"http://{self.expected_host}"
        self.url = f"{self.origin}/{self.session_token}/"

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
                elif route == "api/current":
                    if owner._current is None:
                        self._send_json(HTTPStatus.OK, {"plan": None})
                    else:
                        self._send_json(HTTPStatus.OK, {"plan": owner._current})
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                self.close_connection = True
                if not self._headers_allowed():
                    return
                if self._route() != "api/plan":
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
                    owner._current = build_plan_view(
                        plan,
                        source_name=source_name,
                        hierarchy=owner._hierarchy,
                    ).to_dict()
                except MarvinPilotError as exc:
                    self._send_json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": {"code": exc.exit_code, "message": str(exc)}},
                    )
                    return
                self._send_json(HTTPStatus.OK, {"plan": owner._current})

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
        try:
            self._httpd.serve_forever(poll_interval=0.1)
        finally:
            self._serving = False

    def shutdown(self) -> None:
        """Stop serving and release the loopback port."""

        if self._serving:
            self._httpd.shutdown()
        self._httpd.server_close()
