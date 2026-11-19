# Experimental database engine

This opt-in engine uses Marvin's documented cloud CouchDB access. It is **experimental**,
not a replacement for the default API engine. Back up your account and start on a test
account. No browser, replication process, remote index creation or database setup runs.

## Setup and use

Keep the normal full-access API token: Pilot still verifies the account and uses some
strategy-aware endpoints. Copy the **Sync Server URL, Sync Database, Sync User and Sync
Password from that same account's Settings → API database-access section**.

```console
marvin-pilot config set-database-access
marvin-pilot --experimental-use-db doctor
marvin-pilot --experimental-use-db context search "Example project" --live
marvin-pilot --experimental-use-db prepare draft.json --live --output plan.json
marvin-pilot validate plan.json
marvin-pilot describe plan.json
marvin-pilot --experimental-use-db validate plan.json --live --json
marvin-pilot --experimental-use-db apply plan.json
marvin-pilot --experimental-use-db revert path/to/receipt.json
marvin-pilot config forget-database-access
```

The flag is global: put it **before** the command. It is not persisted. Omitting it uses
the normal API engine, even when database credentials are stored. Offline commands do not
need database credentials. Live failures never silently switch engines and retry writes.

Setup verifies the API account and DB endpoint, then stores the credentials in a separate
OS-keyring service, keyed to that account's ID/email. Password input is hidden; there is
no password CLI argument, environment variable or plaintext-file fallback. These pins
prevent accidentally reusing one stored profile with another API account; they do not
prove ownership of arbitrary credentials you enter. Do not mix accounts' sync settings.
HTTPS and certificate validation are required; credential-bearing URLs and redirects are
refused. Credentials never enter plans or receipts. Removing DB access does not delete
your API token, remote data or local receipts. Revoke it in Marvin if the credential leaks.

## What uses which engine?

| Surface | Experimental behavior |
| --- | --- |
| Account verification, labels | Normal API |
| Today selection | Normal API, then bulk authoritative document reads |
| Live category/project search | Paginated DB query, including completed containers; no API 200-item cap |
| Prepare, live validation, plan-set validation | Batched targets/references/ancestors, then existing local checks |
| Independent ordinary task updates, completions, deletions | `_bulk_docs` with checked `_rev`, up to 100 targets per batch |
| Project/category changes | DB writes, isolated with existing hierarchy/empty-container guards |
| Creates | DB writes, at most one create per request to respect new-item pacing |
| Recurring occurrences and recurrence definitions | Existing API write path, explicitly journaled as compatibility operations |
| Receipt audit and revert preflight | Batched DB reads |
| Revert | Sequential conflict-checked inverse writes; ordinary documents use DB, recurrence uses API |
| Backups, offline review, inline feedback | Unchanged; no new account-wide persistent cache |

The compiler, schema, semantic guards and public approval policy are shared, not duplicated.
API and database requests share a conservative minimum three-second pacing budget in this
mode. Batch reads remove the per-target network bottleneck; they do not promise that every
100-operation plan runs in seconds. Deep ancestors, dependencies, creates, container guards
and recurrence compatibility add requests. The documented average daily cloud budget still
applies; this engine is not permission to bypass it.

## Bulk safety and recovery

Bulk writes are **not atomic**. Every target is read together, checked against its reviewed
fields and revisions, rechecked after approval, and sent with that exact revision. Unknown
native fields are preserved. Conflicted revision leaves are refused, not deleted or merged.
Dependencies and same-target operations create batch barriers. A parent is never deleted
as an implicit cascade. Existing coupled-feature and unattended-impact guards remain active.
Experimental writes require an account-pinned plan; revert requires a pinned receipt.

Before a write, the durable receipt records the full before-images and sending state.
Pilot validates each bulk result and reads targets back together, including deletion
revisions. Partial success stops later batches and leaves per-item applied/failed/unknown
outcomes. A timeout triggers reconciliation, not blind resend. Later human revisions are
not adopted as Pilot's result. Keep private receipts: they are the recovery source.
Write batches are bounded to 8 MiB; oversized batches fail before sending and must be split.

DB deletions, like Pilot API deletions, bypass Marvin's device-local Trash UI. Pilot records
native-style tombstones and can restore the complete original document with the same ID
using a guarded insert into the absent ID (not a tombstone update). Recovery refuses
changed/recreated active targets. Revert
is deliberately sequential in this first version rather than introducing another bulk
inverse state machine. An uncertain receipt may require inspection; never reapply an
entire partial plan without reconciling its per-item outcomes first.

Live DB reads are server-synced state, not unsynced edits on another device. A hierarchy
query paginates, but it is not a transactionally frozen account snapshot. Apply always
uses fresh target revisions instead of trusting context/backup data as write authority.

## Sources

- [Marvin database access](https://help.amazingmarvin.com/amazing-marvin/api-database-access)
- [Marvin rate limits](https://help.amazingmarvin.com/amazing-marvin/api-getting-started#rate-limits)
- [CouchDB bulk APIs and per-document results](https://docs.couchdb.org/en/stable/api/database/bulk-api.html)

Tests use synthetic data, mock HTTP failure/partial/conflict paths, and bounded developer-
account fixtures. No developer credentials or personal task content belong in this repository.
