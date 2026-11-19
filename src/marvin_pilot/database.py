"""Opt-in CouchDB transport. No browser, replication, or implicit remote setup."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from urllib.parse import quote, urlsplit

import httpx
import keyring

from marvin_pilot.compiler import CompiledMutation, project_compiled_mutation
from marvin_pilot.errors import (
    AmbiguousMutationError,
    CredentialError,
    LivePreconditionError,
    RemoteError,
)
from marvin_pilot.marvin_client import MarvinClient, RequestPacer

DATABASE_KEYRING_SERVICE = "marvin-pilot-database"
MAX_DATABASE_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_DATABASE_WRITE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class DatabaseCredentials:
    account_user_id: str
    account_email: str
    server: str
    database: str
    username: str
    password: str = field(repr=False)

    def __post_init__(self):
        values = asdict(self)
        if not all(isinstance(v, str) and 0 < len(v) <= 8192 for v in values.values()):
            raise CredentialError("invalid database credential fields")
        server = urlsplit(self.server)
        if (
            server.scheme != "https"
            or not server.hostname
            or server.username is not None
            or server.password is not None
            or server.query
            or server.fragment
            or server.path not in {"", "/"}
            or self.database in {".", ".."}
            or "/" in self.database
            or "\\" in self.database
            or not self.account_user_id.isdigit()
            or any(ord(c) < 32 for v in values.values() for c in v)
        ):
            raise CredentialError("database access requires an HTTPS server URL and account pin")

    @property
    def url(self) -> str:
        return self.server.rstrip("/") + "/" + quote(self.database, safe="")


def load_database_credentials(account, *, keyring_backend=keyring) -> DatabaseCredentials:
    try:
        raw = keyring_backend.get_password(DATABASE_KEYRING_SERVICE, account.account_user_id)
        if raw is None:
            raise CredentialError(
                "no database credentials for this account; run config set-database-access"
            )
        credentials = DatabaseCredentials(**json.loads(raw))
    except CredentialError:
        raise
    except Exception:
        raise CredentialError("could not read valid account-bound database credentials") from None
    verify_database_account(credentials, account)
    return credentials


def store_database_credentials(
    credentials: DatabaseCredentials, *, keyring_backend=keyring
) -> None:
    try:
        keyring_backend.set_password(
            DATABASE_KEYRING_SERVICE, credentials.account_user_id, json.dumps(asdict(credentials))
        )
    except Exception:
        raise CredentialError("could not store database credentials in the OS keyring") from None


def forget_database_credentials(account, *, keyring_backend=keyring) -> None:
    try:
        keyring_backend.delete_password(DATABASE_KEYRING_SERVICE, account.account_user_id)
    except keyring.errors.PasswordDeleteError:
        return
    except Exception:
        raise CredentialError("could not remove database credentials from the OS keyring") from None


def verify_database_account(credentials, account) -> None:
    if (
        credentials.account_user_id != account.account_user_id
        or credentials.account_email.casefold() != account.account_email.casefold()
    ):
        raise CredentialError("database credentials do not match the verified API account")


class CouchDatabase:
    """Bounded, CAS-based database requests with isolated authentication."""

    def __init__(self, credentials, *, transport=None, pacer=None, timeout=30.0):
        self.credentials = credentials
        self.pacer = pacer or RequestPacer(3000)
        self.client = httpx.Client(
            auth=(credentials.username, credentials.password),
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        )
        self.requests = 0
        self.deleted_revisions: dict[str, str] = {}

    def close(self):
        self.client.close()

    def request(self, method, path="", *, body=None, params=None, mutation=False):
        self.pacer.wait()
        self.requests += 1
        try:
            with self.client.stream(
                method, self.credentials.url + path, json=body, params=params
            ) as response:
                if response.status_code in {401, 403}:
                    raise CredentialError("database authentication/authorization failed")
                if response.status_code == 429:
                    raise RemoteError("database rate limited this request; wait before retrying")
                if not response.is_success:
                    if mutation and response.status_code >= 500:
                        raise AmbiguousMutationError("database write outcome is unknown")
                    raise RemoteError(f"database request failed (HTTP {response.status_code})")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_DATABASE_RESPONSE_BYTES:
                        if mutation:
                            raise AmbiguousMutationError(
                                "database write response exceeded size limit"
                            )
                        raise RemoteError("database response exceeded size limit")
                    chunks.append(chunk)
                return json.loads(b"".join(chunks))
        except (httpx.HTTPError, ValueError):
            if mutation:
                raise AmbiguousMutationError("database write outcome could not be read") from None
            raise RemoteError(
                "database read failed; details suppressed to protect credentials"
            ) from None

    def get_documents(self, identifiers):
        ids = list(dict.fromkeys(identifiers))
        result = {}
        for offset in range(0, len(ids), 200):
            batch = ids[offset : offset + 200]
            value = self.request(
                "POST",
                "/_all_docs",
                body={"keys": batch},
                params={"include_docs": "true", "conflicts": "true"},
            )
            if not isinstance(value, dict) or not isinstance(value.get("rows"), list):
                raise RemoteError("database returned malformed bulk-read rows")
            rows = value["rows"]
            if len(rows) != len(batch):
                raise RemoteError("database bulk read omitted requested rows")
            for expected_id, row in zip(batch, rows, strict=True):
                if not isinstance(row, dict) or row.get("key", row.get("id")) != expected_id:
                    raise RemoteError("database bulk-read identity mismatch")
                if row.get("error") == "not_found":
                    result[expected_id] = None
                    self.deleted_revisions.pop(expected_id, None)
                elif isinstance(row.get("value"), dict) and row["value"].get("deleted") is True:
                    revision = row["value"].get("rev")
                    if not isinstance(revision, str):
                        raise RemoteError("database tombstone is missing its revision")
                    self.deleted_revisions[expected_id] = revision
                    result[expected_id] = None
                elif (
                    isinstance(row.get("doc"), dict)
                    and row["doc"].get("_id") == expected_id
                    and isinstance(row["doc"].get("_rev"), str)
                ):
                    result[expected_id] = row["doc"]
                    self.deleted_revisions.pop(expected_id, None)
                else:
                    raise RemoteError("database returned an invalid document row")
        return result

    def find(self, selector):
        result = []
        seen_ids = set()
        bookmark = None
        for _ in range(200):
            body = {"selector": selector, "limit": 500}
            if bookmark is not None:
                body["bookmark"] = bookmark
            value = self.request("POST", "/_find", body=body)
            docs = value.get("docs") if isinstance(value, dict) else None
            if not isinstance(docs, list):
                raise RemoteError("database returned malformed query results")
            for document in docs:
                if not isinstance(document, dict) or not isinstance(document.get("_id"), str):
                    raise RemoteError("database returned malformed query documents")
                if document["_id"] in seen_ids:
                    raise RemoteError("database pagination repeated a document; retry the read")
                seen_ids.add(document["_id"])
                result.append(document)
            if len(docs) < 500:
                return result
            next_bookmark = value.get("bookmark")
            if not isinstance(next_bookmark, str) or next_bookmark == bookmark:
                raise RemoteError("database query cannot continue without a new bookmark")
            bookmark = next_bookmark
        raise RemoteError("database query exceeded the bounded page limit")

    def validate_write_documents(self, documents):
        if not documents or len(documents) > 200:
            raise ValueError("database write batch must contain 1 to 200 documents")
        ids = [d["_id"] for d in documents]
        if len(set(ids)) != len(ids):
            raise ValueError("database write batch repeats a target")
        if (
            len(json.dumps({"docs": documents}, ensure_ascii=False).encode("utf-8"))
            > MAX_DATABASE_WRITE_BYTES
        ):
            raise LivePreconditionError(
                "database batch exceeds 8 MiB; split the plan into smaller batches"
            )

    def write_documents(self, documents):
        self.validate_write_documents(documents)
        ids = [d["_id"] for d in documents]
        value = self.request("POST", "/_bulk_docs", body={"docs": documents}, mutation=True)
        if not isinstance(value, list) or len(value) != len(ids):
            raise AmbiguousMutationError("database write returned incomplete per-item results")
        by_id = {}
        for row in value:
            if not isinstance(row, dict) or row.get("id") not in ids or row["id"] in by_id:
                raise AmbiguousMutationError("database write returned invalid per-item identities")
            if not (row.get("ok") is True and isinstance(row.get("rev"), str)) and not isinstance(
                row.get("error"), str
            ):
                raise AmbiguousMutationError("database write returned an invalid outcome")
            by_id[row["id"]] = row
        return by_id


class DatabaseMarvinClient:
    """API account/strategy reads plus an explicit, account-bound database engine."""

    categories_complete = True
    experimental_database = True

    def __init__(self, api: MarvinClient, database: CouchDatabase):
        self.api = api
        self.database = database
        self.api_base_host = api.api_base_host
        self.api_base_url = api.api_base_url
        self._bound = False
        self._read_cache: dict[str, dict | None] = {}
        self._last_reads: dict[str, dict | None] = {}

    def close(self):
        self.database.close()
        self.api.close()

    def check_connection(self):
        account = self.api.check_connection()
        verify_database_account(self.database.credentials, account)
        self._bound = True
        return account

    def _require_account(self):
        if not self._bound:
            self.check_connection()

    def clear_read_cache(self):
        self._read_cache.clear()
        self._last_reads.clear()

    def check_database_connection(self):
        self._require_account()
        info = self.database.request("GET")
        if not isinstance(info, dict) or info.get("db_name") != self.database.credentials.database:
            raise CredentialError("database identity did not match the stored credential profile")

    def get_documents(self, identifiers):
        self._require_account()
        return self.database.get_documents(identifiers)

    def prefetch_documents(self, identifiers):
        self._read_cache.update(self.get_documents(identifiers))

    def prefetch_operations(self, operations):
        """Fetch targets/references together, then hydrate ancestor levels in batches."""
        self.clear_read_cache()
        identifiers = set()
        for operation in operations:
            identifiers.add(operation.target.id)
            recurrence = getattr(operation.target, "recurrence", None)
            if recurrence is not None:
                identifiers.add(recurrence.seriesId)
            for field_name in ("before", "after"):
                fields = getattr(operation, field_name, None)
                parent = getattr(fields, "parent", None)
                if parent is not None:
                    identifiers.add(parent.id)
            order = getattr(operation, "siblingOrder", None)
            if order is not None:
                identifiers.update((order.beforeId, order.afterId))
        self._prefetch_hierarchy(identifiers)

    def _prefetch_hierarchy(self, identifiers):
        for _ in range(100):
            identifiers -= {None, "", "root", "unassigned"}
            missing = identifiers - self._read_cache.keys()
            if not missing:
                return
            self.prefetch_documents(sorted(missing))
            identifiers = set()
            for identifier in missing:
                document = self._read_cache[identifier]
                if document is not None:
                    identifiers.update((document.get("parentId"), document.get("recurringTaskId")))
        raise RemoteError("database hierarchy exceeds the bounded ancestor depth")

    def prefetch_revert_operations(self, operations):
        self.clear_read_cache()
        identifiers = {op.targetId for op in operations}
        identifiers.update(op.recurrence.seriesId for op in operations if op.recurrence is not None)
        self.prefetch_documents(sorted(identifiers))

    def get_doc(self, identifier):
        if identifier in self._read_cache:
            document = deepcopy(self._read_cache[identifier])
        else:
            document = self.get_documents([identifier])[identifier]
        self._last_reads[identifier] = deepcopy(document)
        return document

    def get_categories(self):
        self._require_account()
        return self.database.find({"db": "Categories"})

    def get_children(self, parent_id):
        self._require_account()
        return self.database.find({"parentId": parent_id, "db": {"$in": ["Tasks", "Categories"]}})

    def get_labels(self):
        return self.api.get_labels()

    def get_today_items(self, day):
        self._require_account()
        listed = self.api.get_today_items(day)
        self.clear_read_cache()
        self._prefetch_hierarchy({d["_id"] for d in listed if isinstance(d.get("_id"), str)})
        return listed

    def delay_before_reconciled_retry(self, attempt):
        self.api.delay_before_reconciled_retry(attempt)

    def mutation_document(self, compiled: CompiledMutation, current):
        if current is not None and current.get("_conflicts"):
            raise LivePreconditionError("database target has unresolved conflict revisions")
        if compiled.endpoint == "doc/delete":
            if current is None:
                raise LivePreconditionError("database deletion target is absent")
            if current.get("db") == "Categories" and self.get_children(current["_id"]):
                raise LivePreconditionError("database deletion refuses a non-empty container")
            return {
                "_id": current["_id"],
                "_rev": current["_rev"],
                "_deleted": True,
                "db": current["db"],
                "D": 1,
            }
        # A restore is a guarded insert into an absent ID, not an update of the
        # tombstone. Cloudant rejects revival through the tombstone's _rev.
        # An active/recreated ID still conflicts, so no live document is overwritten.
        return project_compiled_mutation(current, compiled)

    def _single_mutation(self, compiled):
        # Use exactly the revision the caller checked, not a newer revision fetched
        # here. CouchDB CAS must reject a race rather than silently adopting it.
        if compiled.target_id not in self._last_reads:
            raise LivePreconditionError("database mutation requires a checked target read")
        current = deepcopy(self._last_reads[compiled.target_id])
        identity = current or compiled.payload
        if identity.get("recurring") is True or identity.get("db") == "RecurringTasks":
            from marvin_pilot.executor import _send_mutation

            # Recurrence writes deliberately retain the tested API semantics.
            try:
                return _send_mutation(self.api, compiled)
            finally:
                self.clear_read_cache()
        if compiled.endpoint == "doc/create" and current is not None:
            raise LivePreconditionError("database create target already exists")
        if compiled.endpoint != "doc/create" and current is None:
            raise LivePreconditionError("database mutation target disappeared")
        result = self.database.write_documents([self.mutation_document(compiled, current)])
        if result[compiled.target_id].get("error"):
            code = result[compiled.target_id]["error"]
            safe_code = code if code in {"conflict", "forbidden", "unauthorized"} else "rejected"
            raise LivePreconditionError(
                f"database refused the write ({safe_code}); refresh its revision"
            )
        self.clear_read_cache()
        document = self.get_doc(compiled.target_id)
        revision = (
            document.get("_rev")
            if document is not None
            else self.database.deleted_revisions.get(compiled.target_id)
        )
        if revision != result[compiled.target_id]["rev"]:
            raise AmbiguousMutationError("database target changed after this write")
        return document

    def create_doc(self, document):
        return self._single_mutation(
            CompiledMutation(
                "db-create", "create", document["_id"], "doc/create", document, document, {}
            )
        )

    def update_doc(self, identifier, setters):
        return self._single_mutation(
            CompiledMutation(
                "db-update", "update", identifier, "doc/update", {"setters": setters}, {}, {}
            )
        )

    def delete_doc(self, identifier):
        return self._single_mutation(
            CompiledMutation("db-delete", "trash", identifier, "doc/delete", {}, {}, {})
        )
