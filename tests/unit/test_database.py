from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from marvin_pilot.database import (
    CouchDatabase,
    DatabaseCredentials,
    DatabaseMarvinClient,
    load_database_credentials,
    store_database_credentials,
)
from marvin_pilot.errors import (
    AmbiguousMutationError,
    CredentialError,
    LivePreconditionError,
    RemoteError,
)
from marvin_pilot.marvin_client import RequestPacer


def credentials(**overrides):
    return DatabaseCredentials(
        **(
            {
                "account_user_id": "123456",
                "account_email": "pilot@example.com",
                "server": "https://database.test",
                "database": "example-db",
                "username": "db-user",
                "password": "synthetic-db-secret",
            }
            | overrides
        )
    )


def database(handler):
    return CouchDatabase(
        credentials(), transport=httpx.MockTransport(handler), pacer=RequestPacer(0)
    )


@pytest.mark.parametrize(
    "server",
    [
        "http://database.test",
        "https://user:password@database.test",
        "https://database.test/other",
        "https://database.test/?key=value",
    ],
)
def test_database_rejects_unsafe_credential_urls(server):
    with pytest.raises(CredentialError):
        credentials(server=server)


def test_password_is_redacted_and_keyring_is_account_bound():
    assert "synthetic-db-secret" not in repr(credentials())
    values = {}
    backend = SimpleNamespace(
        set_password=lambda service, user, value: values.update({(service, user): value}),
        get_password=lambda service, user: values.get((service, user)),
    )
    store_database_credentials(credentials(), keyring_backend=backend)
    account = SimpleNamespace(account_user_id="123456", account_email="pilot@example.com")
    assert load_database_credentials(account, keyring_backend=backend) == credentials()
    with pytest.raises(CredentialError):
        load_database_credentials(
            SimpleNamespace(account_user_id="654321", account_email="other@example.com"),
            keyring_backend=backend,
        )


def test_batch_reads_keep_auth_separate_and_preserve_deleted_revisions():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/example-db/_all_docs"
        assert "X-Full-Access-Token" not in request.headers
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(
            200,
            json={
                "rows": [
                    {"key": "one", "doc": {"_id": "one", "_rev": "1-a", "unknown": 42}},
                    {"key": "missing", "error": "not_found"},
                    {"key": "deleted", "value": {"deleted": True, "rev": "3-b"}},
                ]
            },
        )

    db = database(handle)
    try:
        result = db.get_documents(["one", "missing", "deleted", "one"])
        assert result["one"]["unknown"] == 42 and result["missing"] is None
        assert db.deleted_revisions == {"deleted": "3-b"}
        assert len(calls) == 1
        assert json.loads(calls[0].content)["keys"] == ["one", "missing", "deleted"]
    finally:
        db.close()


def test_find_uses_bookmarks_instead_of_silent_server_truncation():
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        docs = [{"_id": str(i)} for i in range(500)] if len(calls) == 1 else [{"_id": "last"}]
        return httpx.Response(200, json={"docs": docs, "bookmark": "next"})

    db = database(handle)
    try:
        assert len(db.find({"db": "Categories"})) == 501
        assert calls[1]["bookmark"] == "next"
    finally:
        db.close()


def test_find_refuses_repeated_pages():
    db = database(
        lambda _: httpx.Response(
            200, json={"docs": [{"_id": str(i)} for i in range(500)], "bookmark": "same"}
        )
    )
    try:
        with pytest.raises(RemoteError, match="repeated"):
            db.find({"db": "Categories"})
    finally:
        db.close()


@pytest.mark.parametrize(
    "rows", [[], [{"id": "unexpected", "ok": True, "rev": "1-x"}], [{"id": "one"}]]
)
def test_write_requires_every_result_to_have_a_known_identity_and_outcome(rows):
    db = database(lambda _: httpx.Response(201, json=rows))
    try:
        with pytest.raises(AmbiguousMutationError):
            db.write_documents([{"_id": "one"}])
    finally:
        db.close()


def test_conflict_is_a_per_item_result_not_a_whole_batch_success():
    db = database(lambda _: httpx.Response(201, json=[{"id": "one", "error": "conflict"}]))
    try:
        assert db.write_documents([{"_id": "one"}])["one"]["error"] == "conflict"
    finally:
        db.close()


def test_account_mismatch_prevents_all_database_requests():
    db = database(lambda _: pytest.fail("must not access the wrong database"))
    api = SimpleNamespace(
        api_base_host="https://api.test",
        api_base_url="https://api.test/api",
        check_connection=lambda: SimpleNamespace(
            account_user_id="999", account_email="other@example.com"
        ),
        close=lambda: None,
    )
    client = DatabaseMarvinClient(api, db)
    try:
        with pytest.raises(CredentialError, match="match"):
            client.get_doc("one")
        assert db.requests == 0
    finally:
        client.close()


@pytest.mark.parametrize("status", [301, 401, 429, 503])
def test_database_errors_do_not_expose_passwords_or_follow_redirects(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"location": "https://other.test"}, text="synthetic-db-secret"
        )

    db = database(handle)
    try:
        with pytest.raises((CredentialError, RemoteError)) as error:
            db.request("GET")
        assert "synthetic-db-secret" not in str(error.value)
        assert len(calls) == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"rows": []},
        {"rows": [{"key": "wrong"}]},
        {"rows": [{"key": "one", "doc": {"_id": "one"}}]},
    ],
)
def test_malformed_bulk_reads_fail_closed(value):
    db = database(lambda _: httpx.Response(200, json=value))
    try:
        with pytest.raises(RemoteError):
            db.get_documents(["one"])
    finally:
        db.close()


@pytest.mark.parametrize("mutation", [False, True])
def test_malformed_response_preserves_read_or_ambiguous_write_classification(mutation):
    db = database(lambda _: httpx.Response(200, text="not-json"))
    try:
        with pytest.raises(AmbiguousMutationError if mutation else RemoteError):
            db.request("POST", mutation=mutation)
    finally:
        db.close()


@pytest.mark.parametrize(
    "documents", [[], [{"_id": "same"}, {"_id": "same"}], [{"_id": str(i)} for i in range(201)]]
)
def test_invalid_write_batches_do_not_send(documents):
    db = database(lambda _: pytest.fail("invalid batch must never be sent"))
    try:
        with pytest.raises(ValueError):
            db.write_documents(documents)
        assert db.requests == 0
    finally:
        db.close()


def test_oversized_write_batches_fail_before_any_request(monkeypatch):
    monkeypatch.setattr("marvin_pilot.database.MAX_DATABASE_WRITE_BYTES", 32)
    db = database(lambda _: pytest.fail("oversized batch must never be sent"))
    try:
        with pytest.raises(LivePreconditionError, match="split"):
            db.write_documents([{"_id": "one", "note": "synthetic" * 10}])
        assert db.requests == 0
    finally:
        db.close()
