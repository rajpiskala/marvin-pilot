"""Narrow Amazing Marvin full-access client with centralized adaptive pacing."""

from __future__ import annotations

import email.utils
import json
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from marvin_pilot import __version__
from marvin_pilot.errors import (
    AmbiguousMutationError,
    CredentialError,
    RemoteError,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SAFE_ERROR_CHARS = 500


class RequestPacer:
    """Enforce a minimum start interval using a monotonic clock."""

    def __init__(
        self,
        minimum_interval_ms: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.minimum_interval_seconds = minimum_interval_ms / 1_000
        self.clock = clock
        self.sleep = sleep
        self.last_request_started: float | None = None

    def wait(self) -> None:
        """Wait until the next request may start and record its start time."""

        now = self.clock()
        if self.last_request_started is not None:
            remaining = self.minimum_interval_seconds - (now - self.last_request_started)
            if remaining > 0:
                self.sleep(remaining)
        self.last_request_started = self.clock()

    def delay(self, seconds: float) -> None:
        """Honor a server-directed or adaptive delay."""

        if seconds > 0:
            self.sleep(seconds)


class MarvinClient:
    """Expose only the documented document get/update/create endpoints needed by v1."""

    def __init__(
        self,
        full_access_token: str,
        *,
        api_base_url: str = "https://serv.amazingmarvin.com/api",
        timeout_seconds: float = 20,
        minimum_request_interval_ms: int = 750,
        transport: httpx.BaseTransport | None = None,
        pacer: RequestPacer | None = None,
        jitter: Callable[[], float] = random.random,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = urlparse(api_base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("api_base_url must be an HTTPS origin/path without query or fragment")
        self._token = full_access_token
        self._jitter = jitter
        self._max_response_bytes = max_response_bytes
        self._pacer = pacer or RequestPacer(minimum_request_interval_ms)
        self._client = httpx.Client(
            base_url=api_base_url.rstrip("/") + "/",
            headers={
                "X-Full-Access-Token": full_access_token,
                "User-Agent": f"marvin-pilot/{__version__}",
                "Accept": "application/json",
            },
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    @property
    def api_base_host(self) -> str:
        """Return a receipt-safe origin without credentials."""

        url = self._client.base_url
        return f"{url.scheme}://{url.host}" + (f":{url.port}" if url.port else "")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MarvinClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _safe_response_text(self, response: httpx.Response) -> str:
        content = response.content[:MAX_SAFE_ERROR_CHARS]
        rendered = content.decode("utf-8", errors="replace").replace(self._token, "[REDACTED]")
        return rendered.replace("\r", " ").replace("\n", " ")

    def _retry_after_seconds(self, response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(0.0, float(header))
            except ValueError:
                try:
                    retry_at = email.utils.parsedate_to_datetime(header)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(30.0, (2**attempt) + self._jitter())

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        mutation: bool = False,
        allowed_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        max_attempts = 4
        for attempt in range(max_attempts):
            self._pacer.wait()
            try:
                response = self._client.request(method, endpoint, params=params, json=payload)
            except httpx.TimeoutException as exc:
                if mutation:
                    raise AmbiguousMutationError(
                        f"{method} {endpoint} timed out; remote outcome must be reconciled"
                    ) from exc
                if attempt + 1 < max_attempts:
                    self._pacer.delay(min(30.0, (2**attempt) + self._jitter()))
                    continue
                raise RemoteError(f"{method} {endpoint} timed out") from exc
            except httpx.HTTPError as exc:
                raise RemoteError(f"{method} {endpoint} failed: {type(exc).__name__}") from exc

            if len(response.content) > self._max_response_bytes:
                raise RemoteError(
                    f"{method} {endpoint} returned more than {self._max_response_bytes} bytes"
                )
            if response.status_code in {401, 403}:
                raise CredentialError(
                    "Amazing Marvin rejected the full-access credential "
                    f"(HTTP {response.status_code})"
                )
            if response.status_code in allowed_statuses:
                return response
            retryable = response.status_code == 429 or (
                not mutation and response.status_code == 503
            )
            if retryable and attempt + 1 < max_attempts:
                self._pacer.delay(self._retry_after_seconds(response, attempt))
                continue
            if response.status_code >= 400:
                detail = self._safe_response_text(response)
                suffix = f": {detail}" if detail else ""
                raise RemoteError(
                    f"{method} {endpoint} returned HTTP {response.status_code}{suffix}"
                )
            return response
        raise AssertionError("request retry loop exhausted without returning or raising")

    def _json_value(self, response: httpx.Response, *, required_object: bool) -> Any:
        if response.status_code == 204 or not response.content:
            if required_object:
                raise RemoteError("Amazing Marvin returned an empty document response")
            return None
        try:
            value = response.json()
        except json.JSONDecodeError as exc:
            raise RemoteError("Amazing Marvin returned malformed JSON") from exc
        if required_object and not isinstance(value, dict):
            raise RemoteError("Amazing Marvin returned a non-object document response")
        return value

    def get_doc(self, item_id: str) -> dict[str, Any] | None:
        """Fetch one document by encoded ID; return ``None`` for HTTP 404."""

        response = self._request(
            "GET", "doc", params={"id": item_id}, allowed_statuses=frozenset({404})
        )
        if response.status_code == 404:
            return None
        return self._json_value(response, required_object=True)

    def update_doc(self, item_id: str, setters: list[dict[str, Any]]) -> Any:
        """Update multiple fields on exactly one document."""

        response = self._request(
            "POST",
            "doc/update",
            payload={"itemId": item_id, "setters": setters},
            mutation=True,
        )
        return self._json_value(response, required_object=False)

    def create_doc(self, document: dict[str, Any]) -> Any:
        """Create exactly one reviewed task document."""

        response = self._request("POST", "doc/create", payload=document, mutation=True)
        return self._json_value(response, required_object=False)
