from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from time import sleep

import httpx
import pytest

from marvin_pilot.errors import AmbiguousMutationError, LivePreconditionError, RemoteError
from marvin_pilot.native import NativeAccount, NativeBridge, NativeDatabase, check_native_task

ACCOUNT = NativeAccount("123", "pilot@example.com")
SECRET = "synthetic-test-pairing"
IDENTITY = {
    "tab": "first-tab-12345678900000",
    "protocol": 1,
    "userId": ACCOUNT.account_user_id,
    "email": ACCOUNT.account_email,
}


@pytest.fixture
def bridge():
    value = NativeBridge(ACCOUNT, SECRET, port=0, timeout=2, long_poll=0.05)
    try:
        yield value
    finally:
        value.close()


def test_http_auth_account_tab_and_receipt_claim_are_all_required(bridge):
    with httpx.Client(base_url="http://" + bridge.host, trust_env=False) as http:
        assert http.post("/next", json=IDENTITY).status_code == 403
        http.headers["Authorization"] = "Bearer " + SECRET
        assert http.post("/next", json=IDENTITY | {"email": "wrong@example.com"}).status_code == 409
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(bridge.check_connection)
            lease = http.post("/next", json=IDENTITY).json()["command"]
            assert lease["method"] == "ping"
            assert http.post("/next", json=IDENTITY).json() == {"command": None}
            assert (
                http.post("/next", json=IDENTITY | {"tab": "other-tab-12345678900000"}).status_code
                == 409
            )
            reply = IDENTITY | {"id": lease["id"], "ok": True, "data": {"ready": True}}
            assert http.post("/reply", json=reply | {"id": "wrong"}).status_code == 409
            assert http.post("/reply", json=reply).status_code == 200
            assert pending.result(2) == ACCOUNT
            assert http.post("/reply", json=reply).status_code == 409


def test_no_automatic_replay_when_write_reply_is_lost():
    bridge = NativeBridge(ACCOUNT, SECRET, port=0, timeout=0.3, long_poll=0.03)
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(bridge.call, "write", {"docs": [{}]}, mutation=True)
            claim = bridge.dispatch("/next", IDENTITY)[1]["command"]
            assert claim["method"] == "write"
            assert bridge.dispatch("/next", IDENTITY)[1] == {"command": None}
            with pytest.raises(AmbiguousMutationError, match="not confirmed"):
                future.result(2)
            assert bridge.dispatch("/reply", IDENTITY | {"id": claim["id"], "ok": True})[0] == 409
            assert bridge.dispatch("/next", IDENTITY)[1] == {"command": None}
    finally:
        bridge.close()


def test_no_browser_is_a_read_failure_not_a_mutation_claim():
    bridge = NativeBridge(ACCOUNT, SECRET, port=0, timeout=0.02)
    try:
        with pytest.raises(RemoteError, match="no reply"):
            bridge.call("write", {}, mutation=True)
    finally:
        bridge.close()


def test_document_transport_reuses_bulk_row_identity_and_revision_validation(bridge):
    db = NativeDatabase(bridge)
    errors = []

    def browser():
        try:
            while True:
                command = bridge.dispatch("/next", IDENTITY)[1]["command"]
                if command:
                    break
                sleep(0.01)
            assert command["data"] == {"keys": ["task"]}
            bridge.dispatch(
                "/reply",
                IDENTITY
                | {
                    "id": command["id"],
                    "ok": True,
                    "data": {"rows": [{"key": "task", "value": {"deleted": True, "rev": "2-x"}}]},
                },
            )
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=browser, daemon=True)
    thread.start()
    assert db.get_documents(["task"]) == {"task": None}
    thread.join(2)
    assert not errors and not thread.is_alive()
    assert db.deleted_revisions == {"task": "2-x"}


@pytest.mark.parametrize("field", ["recurring", "reminders", "calId", "done", "dependencies"])
def test_native_refuses_coupled_or_completed_tasks(field):
    with pytest.raises(LivePreconditionError, match="ordinary open task"):
        check_native_task({"db": "Tasks", field: True})
