from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from marvin_pilot.errors import (
    AmbiguousMutationError,
    AmbiguousServerResponseError,
    CredentialError,
    RemoteError,
)
from marvin_pilot.marvin_client import MarvinClient, RequestPacer


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def json_response(status: int, value: Any, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, json=value, **kwargs)


def client_for(
    handler,
    *,
    token: str = "full-secret-token",
    pacer: RequestPacer | None = None,
    max_response_bytes: int = 2 * 1024 * 1024,
) -> MarvinClient:
    effective_pacer = pacer or RequestPacer(0, sleep=lambda _seconds: None)
    return MarvinClient(
        token,
        api_base_url="https://marvin.test/api",
        minimum_request_interval_ms=0,
        transport=httpx.MockTransport(handler),
        pacer=effective_pacer,
        jitter=lambda: 0.0,
        max_response_bytes=max_response_bytes,
    )


def test_get_doc_encodes_id_and_uses_only_full_access_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, {"_id": "task/id + ?", "db": "Tasks"})

    with client_for(handler) as client:
        document = client.get_doc("task/id + ?")
        assert client.api_base_host == "https://marvin.test"
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/doc"
    assert request.url.params["id"] == "task/id + ?"
    assert request.headers["X-Full-Access-Token"] == "full-secret-token"
    assert "X-API-Token" not in request.headers
    assert document == {"_id": "task/id + ?", "db": "Tasks"}


def test_get_doc_returns_none_for_404() -> None:
    with client_for(lambda _request: httpx.Response(404)) as client:
        assert client.get_doc("missing") is None


def test_get_doc_returns_none_for_marvin_200_missing_document() -> None:
    response = json_response(200, {"error": "not_found", "reason": "missing"})
    with client_for(lambda _request: response) as client:
        assert client.get_doc("missing") is None


def test_get_doc_returns_none_for_marvin_200_deleted_tombstone() -> None:
    response = json_response(200, {"error": "not_found", "reason": "deleted"})
    with client_for(lambda _request: response) as client:
        assert client.get_doc("deleted") is None


def test_get_doc_rejects_other_200_error_documents() -> None:
    response = json_response(200, {"error": "unexpected", "reason": "bad state"})
    with (
        client_for(lambda _request: response) as client,
        pytest.raises(RemoteError, match="unexpected: bad state"),
    ):
        client.get_doc("task")


@pytest.mark.parametrize(("status", "reason"), [(200, "OK"), (201, "Created")])
def test_connection_check_returns_account_and_http_metadata_from_read_only_me(
    status: int, reason: str
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(status, {"email": "pilot@example.com", "userId": "123456"})

    with client_for(handler) as client:
        check = client.check_connection()

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/me"
    assert not request.url.params
    assert request.content == b""
    assert request.headers["X-Full-Access-Token"] == "full-secret-token"
    assert "X-API-Token" not in request.headers
    assert check.account_email == "pilot@example.com"
    assert check.account_user_id == "123456"
    assert check.method == "GET"
    assert check.request_url == "https://marvin.test/api/me"
    assert check.status_code == status
    assert check.reason_phrase == reason


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"userId": "123456"},
        {"email": "pilot@example.com"},
        {"email": "bad\nemail", "userId": "123456"},
        {"email": "pilot@example.com", "userId": "not-numeric"},
    ],
)
def test_connection_check_rejects_malformed_account_identity(value: object) -> None:
    with (
        client_for(lambda _request: json_response(200, value)) as client,
        pytest.raises(RemoteError) as error,
    ):
        client.check_connection()
    assert error.value.http_status_code == 200


def test_get_labels_uses_full_access_header_and_validates_metadata() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, [{"_id": "label-a", "title": "Math"}])

    with client_for(handler) as client:
        assert client.get_labels() == [{"_id": "label-a", "title": "Math"}]
    assert seen[0].url.path == "/api/labels"
    assert seen[0].headers["X-Full-Access-Token"] == "full-secret-token"
    assert "X-API-Token" not in seen[0].headers


@pytest.mark.parametrize("value", [{"labels": []}, ["not-an-object"], [{"title": "No ID"}]])
def test_get_labels_rejects_malformed_metadata(value: object) -> None:
    with (
        client_for(lambda _request: json_response(200, value)) as client,
        pytest.raises(RemoteError, match="malformed label metadata"),
    ):
        client.get_labels()


def test_update_sends_one_item_with_all_setters() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return json_response(200, {"ok": True})

    setters = [
        {"key": "title", "val": "New"},
        {"key": "fieldUpdates.title", "val": 123},
        {"key": "updatedAt", "val": 123},
    ]
    with client_for(handler) as client:
        assert client.update_doc("task-a", setters) == {"ok": True}
    assert seen == [{"itemId": "task-a", "setters": setters}]


def test_create_sends_reviewed_document_and_accepts_empty_success() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(204)

    document = {"_id": "new-id", "db": "Tasks", "title": "New"}
    with client_for(handler) as client:
        assert client.create_doc(document) is None
    assert seen == [document]


def test_delete_sends_exact_item_id_and_accepts_empty_success() -> None:
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    with client_for(handler) as client:
        assert client.delete_doc("task-to-delete") is None
    assert seen == [("POST", "/api/doc/delete", {"itemId": "task-to-delete"})]


def test_pacer_enforces_monotonic_start_interval() -> None:
    fake = FakeTime()
    pacer = RequestPacer(750, clock=fake.clock, sleep=fake.sleep)
    pacer.wait()
    fake.now += 0.25
    pacer.wait()
    fake.now += 1.0
    pacer.wait()
    assert fake.sleeps == [0.5]


def test_reconciled_mutation_retry_uses_capped_exponential_backoff() -> None:
    fake = FakeTime()
    pacer = RequestPacer(0, clock=fake.clock, sleep=fake.sleep)
    with client_for(lambda _request: json_response(200, {}), pacer=pacer) as client:
        client.delay_before_reconciled_retry(0)
        client.delay_before_reconciled_retry(1)
        client.delay_before_reconciled_retry(8)
    assert fake.sleeps == [1.0, 2.0, 30.0]


def test_get_retries_429_and_honors_retry_after() -> None:
    fake = FakeTime()
    pacer = RequestPacer(0, clock=fake.clock, sleep=fake.sleep)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2.5"})
        return json_response(200, {"_id": "task-a"})

    with client_for(handler, pacer=pacer) as client:
        assert client.get_doc("task-a") == {"_id": "task-a"}
    assert calls == 2
    assert fake.sleeps == [2.5]


def test_get_retries_http_200_logical_rate_limit_with_exponential_backoff() -> None:
    fake = FakeTime()
    pacer = RequestPacer(0, clock=fake.clock, sleep=fake.sleep)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return json_response(
                200,
                {"error": "too_many_requests", "reason": "try again later"},
            )
        return json_response(200, {"_id": "task-a"})

    with client_for(handler, pacer=pacer) as client:
        assert client.get_doc("task-a") == {"_id": "task-a"}
    assert calls == 3
    assert fake.sleeps == [1.0, 2.0]


def test_mutation_retries_http_200_logical_rate_limit() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json_response(200, {"error": "too_many_requests"})
        return json_response(200, {"ok": True})

    with client_for(handler) as client:
        assert client.update_doc("task", []) == {"ok": True}
    assert calls == 2


def test_logical_rate_limit_stops_after_bounded_attempts() -> None:
    fake = FakeTime()
    pacer = RequestPacer(0, clock=fake.clock, sleep=fake.sleep)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response(200, {"error": "too_many_requests"})

    with (
        client_for(handler, pacer=pacer) as client,
        pytest.raises(RemoteError, match="rate limited after 6 attempts") as error,
    ):
        client.get_doc("task-a")
    assert error.value.http_status_code == 200
    assert calls == 6
    assert fake.sleeps == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_get_retries_503_with_capped_backoff() -> None:
    fake = FakeTime()
    pacer = RequestPacer(0, clock=fake.clock, sleep=fake.sleep)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls < 3 else json_response(200, {"_id": "task-a"})

    with client_for(handler, pacer=pacer) as client:
        client.get_doc("task-a")
    assert calls == 3
    assert fake.sleeps == [1.0, 2.0]


def test_mutation_retries_explicit_429_but_reconciles_503() -> None:
    calls = 0

    def rate_limited(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429) if calls == 1 else json_response(200, {"ok": True})

    with client_for(rate_limited) as client:
        assert client.update_doc("task", []) == {"ok": True}
    assert calls == 2

    with (
        client_for(lambda _request: httpx.Response(503, text="unavailable")) as client,
        pytest.raises(AmbiguousServerResponseError, match="must be reconciled"),
    ):
        client.create_doc({"_id": "task"})


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_have_stable_credential_error(status: int) -> None:
    with (
        client_for(lambda _request: httpx.Response(status, text="no")) as client,
        pytest.raises(CredentialError, match=f"HTTP {status}") as error,
    ):
        client.get_doc("task")
    assert error.value.http_status_code == status


def test_server_error_redacts_token_and_flattens_response() -> None:
    token = "top-secret"
    response_text = f"bad token {token}\nsecond line"
    with (
        client_for(lambda _request: httpx.Response(500, text=response_text), token=token) as client,
        pytest.raises(RemoteError) as error,
    ):
        client.get_doc("task")
    assert token not in str(error.value)
    assert "[REDACTED] second line" in str(error.value)
    assert error.value.http_status_code == 500


def test_oversized_and_malformed_responses_are_rejected() -> None:
    with (
        client_for(
            lambda _request: httpx.Response(200, content=b"12345"), max_response_bytes=4
        ) as client,
        pytest.raises(RemoteError, match="more than 4 bytes"),
    ):
        client.get_doc("task")
    with (
        client_for(lambda _request: httpx.Response(200, text="not-json")) as client,
        pytest.raises(RemoteError, match="malformed JSON"),
    ):
        client.get_doc("task")


@pytest.mark.parametrize("response", [httpx.Response(204), json_response(200, ["not", "object"])])
def test_get_requires_object_document(response: httpx.Response) -> None:
    with (
        client_for(lambda _request: response) as client,
        pytest.raises(RemoteError, match="document response"),
    ):
        client.get_doc("task")


def test_mutation_timeout_is_ambiguous_and_never_blindly_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("uncertain", request=request)

    with (
        client_for(handler) as client,
        pytest.raises(AmbiguousMutationError, match="must be reconciled"),
    ):
        client.update_doc("task", [])
    assert calls == 1


def test_get_timeout_retries_then_fails() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("retry", request=request)

    with client_for(handler) as client, pytest.raises(RemoteError, match="timed out"):
        client.get_doc("task")
    assert calls == 4


def test_non_timeout_transport_error_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("internal network detail", request=request)

    with client_for(handler) as client, pytest.raises(RemoteError, match="ConnectError") as error:
        client.get_doc("task")
    assert "internal network detail" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    ["http://marvin.test/api", "https:///missing-host", "https://marvin.test/api?q=bad"],
)
def test_base_url_must_be_safe_https(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        MarvinClient("token", api_base_url=url)
