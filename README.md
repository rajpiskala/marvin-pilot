# Marvin Pilot

**Your AI plans. You approve. Marvin Pilot applies.**

Your [Amazing Marvin MCP](https://github.com/bgheneti/Amazing-Marvin-MCP) can understand
your workload. Marvin Pilot lets it safely reorganize that workload without giving the AI your
full-access key.

Marvin Pilot is a separate, local approval CLI. An AI drafts a strict JSON change plan using task
data read through the limited-access MCP. You inspect the exact diff, then run the mutating command
yourself. Marvin Pilot checks live state, asks once for confirmation, applies one task at a time,
verifies each result, and saves an integrity-checked receipt that supports complete or selective
compensating revert.

> [!WARNING]
> Marvin Pilot is pre-alpha. The complete v1 lifecycle and a 200-operation scale run were verified
> against a dedicated Amazing Marvin development account on 2026-08-08, but the client has not yet
> seen enough account shapes and upstream conditions for production use. Back up Marvin and use a
> non-production account while evaluating it.

## The workflow

```text
Amazing Marvin MCP (limited key) -> AI-authored plan.json -> human review
                                                        -> marvin-pilot apply
                                                           (full key + receipt)
                                                        -> marvin-pilot revert
                                                           (full key + new receipt)
```

```console
# Safe for an AI or human: offline, no credential, no network
marvin-pilot validate plan.json
marvin-pilot describe plan.json

# Human-only: live preflight, one [y/N] prompt, mutation, verification, receipt
marvin-pilot apply plan.json

# Revert every successfully applied operation, in reverse application order
marvin-pilot revert path/to/applied-receipt.json

# Or revert several selected operations in one command
marvin-pilot revert path/to/applied-receipt.json \
  --only improve-dinner-task \
  --only reschedule-wash-dishes

# The exact original plan can also resolve its matching apply receipt
marvin-pilot revert plan.json --only reschedule-wash-dishes
```

Revert is deliberately conflict-aware. It restores only fields the apply changed, preserves
unrelated later edits, and refuses to proceed if a touched field has since changed. It creates a
new append-oriented receipt; it never rewrites the apply receipt.

## Installation for development

Marvin Pilot requires Python 3.11 or newer.

```console
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/marvin-pilot --help
```

On Windows, use `.venv\Scripts\python.exe` and `.venv\Scripts\marvin-pilot.exe`.

The planned public distribution name is `amazing-marvin-pilot`; PyPI publishing has not begun.

## Full-access credential setup

Run the guided setup:

```console
marvin-pilot config
```

The recommended mode uses the native OS credential service: macOS Keychain, Windows Credential
Locker, or a supported Linux Secret Service/KWallet backend. The ordinary TOML config stores no
token.

```console
marvin-pilot config set-credential-mode keyring
marvin-pilot config set-full-access-token       # hidden input
marvin-pilot config show                         # never prints the token
marvin-pilot config paths
```

Two stricter alternatives are available:

- `prompt` asks for the token through hidden terminal input on every live command.
- `file` reads a token from a carefully permissioned file. A one-command override is
  `--full-access-key-file PATH`.

There is intentionally no `--full-access-key VALUE`, token environment variable, `--yes`, raw
setter escape hatch, or non-interactive mutation command.

## Change plans

Plans are versioned, closed-schema JSON objects containing stable `operationId` values and typed
`update`, `create`, or `trash` operations. Generate authoritative material directly from the CLI:

```console
marvin-pilot example --output plan.json
marvin-pilot schema --output change-plan.schema.json
marvin-pilot help plan-format
```

V1 supports common task fields including title, parent/category, scheduling and date fields,
labels, `estimatedTimeDuration` (mapped to Marvin `timeEstimate`), note, ranks, sections,
star/frog priority, backburner, review date, snooze values, and dependencies. JSON `null` clears a
supported value; `scheduledDate: null` unschedules a task.

Permanent deletion is not implemented. A `trash` operation uses Marvin's reversible UI-style
Trash fields through `/doc/update`; the client exposes no `/doc/delete` or purge method. Coupled
recurrence, pinned, reward, reminder, calendar, and active-tracking behaviors are blocked in v1.

## Audit history and recovery

The history directory is platform-correct and configurable. It receives a pending journal before
the first write, then a terminal `applied-*`, `partial-*`, `failed-*`, `reverted-*`,
`partial-revert-*`, or `failed-revert-*` receipt. Receipts include the exact source plan, canonical
digest, requests without credentials, field-scoped before/after snapshots, and per-operation
outcomes.

```console
marvin-pilot history path
marvin-pilot history list
marvin-pilot history show latest
marvin-pilot history verify path/to/receipt.json
# Terminalize a safely stopped pending journal before resuming from its partial receipt
marvin-pilot history finalize-interrupted path/to/pending-receipt.json
marvin-pilot config set-history-dir /private/location
```

The SHA-256 receipt hash detects accidental modification; it is not a signature and does not make
the history tamper-proof.

## Safety boundary

The AI should generate, validate, and describe plans. It should not invoke `apply` or `revert`.
Those commands require an interactive controlling terminal and default to no.

This is a strong workflow boundary, not an OS sandbox. Software running as the same user may be
able to invoke the CLI and, depending on the platform, ask the user's credential service for the
stored key. Use prompt or key-file mode when that threat matters.

## Development and tests

```console
.venv/bin/python -m pytest --cov=marvin_pilot
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m ruff check src tests
```

The suite covers strict schema validation, every field mapping, credential modes, HTTP redaction,
capped exponential backoff with jitter, full live preflight, journal-first apply, partial failure,
ambiguous-response reconciliation, full/selective revert, conflict detection, CLI workflows, and
history integrity. Dedicated-account API and browser contract testing has also covered every v1
field plus create, schedule, unschedule, Trash, restore, revert, and a 200-operation scale run. See
[`docs/live-contract-test-report.md`](docs/live-contract-test-report.md) for sanitized results.

The successful live cases are packaged as a reusable contract-test kit. It generates fresh,
isolated plans for another development account, bundles schema-valid sample plans and schemas,
records expected results and hashes in a manifest, and verifies the entire suite offline:

```console
marvin-pilot contract-tests verify contract-tests/samples
marvin-pilot contract-tests generate plans/my-contract-run \
  --base-date 2026-08-10 \
  --account-config plans/contract-account.json
```

See [`contract-tests/README.md`](contract-tests/README.md) before running any live case. The bundled
sample plans are deliberately marked `DO NOT APPLY`; live suites must be freshly generated into
ignored local storage.

The full research, API mapping, threat model, design decisions, and rollout gates are in
[`Implementation-Plan.md`](Implementation-Plan.md).

Marvin Pilot is an independent community project and is not affiliated with or endorsed by Amazing
Marvin.
