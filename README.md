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

## Getting started

The finished setup has two deliberately separate credentials:

| Credential | Used by | What it can do | Where this setup stores it |
| --- | --- | --- | --- |
| Marvin `API_TOKEN` | Amazing Marvin MCP | Read data and use Marvin's limited API | Native OS keyring through this repository's MCP launcher |
| Marvin `FULL_ACCESS_TOKEN` | Marvin Pilot | Apply and revert reviewed change plans | Native OS keyring, or a stricter prompt/key-file mode |

Never give the full-access token to the MCP or the AI. Both credentials should come from the same
Marvin account.

### 1. Install Marvin Pilot and make the command available

Marvin Pilot currently installs from source and requires Python 3.11 or newer:

```console
git clone https://github.com/rajpiskala/marvin-pilot.git
cd marvin-pilot
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\marvin-pilot.exe --version
```

To call that executable as `marvin-pilot` without activating the virtual environment, add its
`Scripts` directory to your user PATH. Run this once from the repository root:

```powershell
$pilotScripts = (Resolve-Path .\.venv\Scripts).Path
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @($userPath -split ";" | Where-Object { $_ })
if ($pathEntries -notcontains $pilotScripts) {
    $newUserPath = ($pathEntries + $pilotScripts) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
}
$env:Path = "$pilotScripts;$env:Path"
marvin-pilot --help
```

Windows recognizes `marvin-pilot.exe` as `marvin-pilot`; no wrapper or renamed copy is needed. Open
a new terminal before testing the persistent PATH. If the repository moves later, update the PATH
entry.

On macOS or Linux:

```console
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
export PATH="$PWD/.venv/bin:$PATH"
marvin-pilot --help
```

Add the same **absolute** `.venv/bin` path to `~/.zshrc`, `~/.bashrc`, or the appropriate shell
profile to make it persistent.

### 2. Get both credentials from Marvin

In Amazing Marvin, open **Settings → API** (or use Marvin's
[direct API settings link](https://app.amazingmarvin.com/pre?api=)) and enable API access. Copy
both values shown there:

- `API_TOKEN`, sometimes described as the API key or limited-access token
- `FULL_ACCESS_TOKEN`, the dangerous credential used for arbitrary document changes

The [Amazing Marvin API wiki](https://github.com/amazingmarvin/MarvinAPI/wiki/Marvin-API#credentials)
defines the two credential levels, and the
[MCP setup guide](https://github.com/bgheneti/Amazing-Marvin-MCP#quick-start-2-minutes)
shows the Settings → API location. Rotate either token from Marvin's API settings if it is ever
exposed. Do not save credentials in this repository, a plan, a receipt, a screenshot, or an AI
chat.

### 3. Configure the MCP with only the limited token

Use a separate virtual environment so the MCP can be upgraded independently:

```powershell
# Windows PowerShell
python -m venv .tools\amazing-marvin-mcp
.\.tools\amazing-marvin-mcp\Scripts\python.exe -m pip install --upgrade pip
.\.tools\amazing-marvin-mcp\Scripts\python.exe -m pip install amazing-marvin-mcp keyring
.\.tools\amazing-marvin-mcp\Scripts\python.exe tools\amazing_marvin_mcp_launcher.py --store-key
```

```console
# macOS or Linux
python -m venv .tools/amazing-marvin-mcp
.tools/amazing-marvin-mcp/bin/python -m pip install --upgrade pip
.tools/amazing-marvin-mcp/bin/python -m pip install amazing-marvin-mcp keyring
.tools/amazing-marvin-mcp/bin/python tools/amazing_marvin_mcp_launcher.py --store-key
```

At the hidden prompt, enter the limited `API_TOKEN`—not the full-access token. The launcher stores
it under `amazing-marvin-mcp / limited-api-key` in macOS Keychain, Windows Credential Locker, or a
supported Linux Secret Service/KWallet backend. It only supplies the token to the MCP child process
when that process starts.

For Codex, register the launcher as a local STDIO MCP server. Use absolute paths so it works no
matter which folder Codex opens in:

```powershell
# Windows PowerShell, from this repository
$mcpPython = (Resolve-Path .\.tools\amazing-marvin-mcp\Scripts\python.exe).Path
$mcpLauncher = (Resolve-Path .\tools\amazing_marvin_mcp_launcher.py).Path
codex mcp add amazing-marvin -- $mcpPython $mcpLauncher
```

```console
# macOS or Linux, from this repository
codex mcp add amazing-marvin -- \
  "$PWD/.tools/amazing-marvin-mcp/bin/python" \
  "$PWD/tools/amazing_marvin_mcp_launcher.py"
```

For another MCP client, use the same absolute Python path as its STDIO `command` and the launcher's
absolute path as its only argument. Do not add an `AMAZING_MARVIN_API_KEY` value to that client's
configuration—the launcher is intentionally responsible for retrieving it from the keyring.

Restart the AI client after registering the server. In Codex, open `/mcp`, confirm that
`amazing-marvin` is enabled, and ask:

> Use the Amazing Marvin MCP's `test_api_connection` and `get_account_info` tools. Tell me which
> account is connected. Do not create, update, complete, or delete anything.

Confirm that it is the intended account before continuing. To replace the limited token later,
rerun the launcher with `--store-key`, then restart the AI client.

### 4. Configure Marvin Pilot with the full-access token

The recommended setup uses the OS keyring and hidden terminal input:

```console
marvin-pilot config set-credential-mode keyring
marvin-pilot config set-full-access-token
marvin-pilot config show
marvin-pilot config paths
```

Enter the `FULL_ACCESS_TOKEN` at the hidden prompt. `config show` deliberately never prints it. Do
not pass it as a command-line argument or environment variable; Marvin Pilot intentionally offers
neither interface.

Check `marvin-pilot history path` as well. The audit directory contains task titles and before/after
state, so choose a durable, private location if the platform default is unsuitable:

```console
marvin-pilot config set-history-dir /private/durable/location
```

On Windows, that path might instead be something like
`C:\Users\you\Documents\Marvin Pilot History`.

### 5. Run a small, reversible first plan

Back up or export Marvin first. Then ask the AI for a small plan before tackling a large cleanup:

> Read my Marvin tasks through the Amazing Marvin MCP. Draft a Marvin Pilot change plan with at
> most 10 low-risk operations and save it under `plans/`. Do not use MCP mutation tools and do not
> run `marvin-pilot apply` or `marvin-pilot revert`; I will review and run those myself.

The repository ignores `plans/` because plans contain private task data; keep that directory out of
source control when using a different checkout or workflow.

Review the resulting file through more than one representation:

```console
marvin-pilot validate plans/first-plan.json
marvin-pilot describe plans/first-plan.json
marvin-pilot visualize plans/first-plan.json
```

The visualizer keeps running until Ctrl+C, so use a second terminal or stop it before applying.
When every row looks correct, apply it yourself:

```console
marvin-pilot apply plans/first-plan.json
marvin-pilot history list
```

Save the applied receipt path printed by the command. If the result is wrong, revert that receipt
(or select several operation IDs with repeated `--only` options):

```console
marvin-pilot revert path/to/applied-receipt.json
marvin-pilot revert path/to/applied-receipt.json \
  --only operation-one \
  --only operation-two
```

For a 100-task reorganization, use reviewed batches of roughly 15–25 operations. Smaller receipts
are easier to audit and selectively revert, and a first small batch catches account, category-ID,
and workflow mistakes before they affect the whole backlog.

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

## Visual review

Open any valid change plan in the credential-free browser visualizer:

```console
marvin-pilot visualize plan.json

# Or open the file-picker landing page
marvin-pilot visualize
```

The page previews **Now** and **After (preview)** as one row-aligned, GitHub-style task diff. A
shared section contains both sides of each Update; a task moved from Inbox to People appears under
an `Inbox → People` transition heading. Creates leave a blank Now cell and Trash operations leave
a blank After cell, so every operation remains horizontally paired and every following section
starts at the same height. Review the diff in **Side by side** or switch to **Now only** or **After only**; the three
compact header icons select Light, Dusk, or Night. Click the Create, Update, or Trash total to show
only that action, then click it again to restore the full plan. Each task's quiet details affordance
opens its reason, identifiers, and exact JSON field diff. Timed rows are ordered chronologically
within a section using the proposed title time first; untimed rows retain Create → Update → Trash
order. Display order is deterministic; apply still uses original plan order.

The headline comes from the plan's required top-level `summary` field; it is plan context generated
alongside the operations, not visualizer-authored copy.

The visualizer runs on an ephemeral `127.0.0.1` URL, accepts at most one 4 MiB JSON plan, and sends
the bytes to the same strict Python validator used by `apply`. It loads no Marvin credential,
makes no Marvin API request, stores no plan or task content in browser storage, and exposes no
mutation control. Only the selected theme and comparison-layout preferences persist. Press
Ctrl+C in the launching terminal to stop it; use `--no-open` when you want to open the printed URL
yourself.

Optional plan-only section hints make sparse update cards easier to group without affecting apply:

```json
"display": {
  "beforeSection": "Inbox",
  "afterSection": "People"
}
```

See [`docs/visualizer-test-matrix.md`](docs/visualizer-test-matrix.md) for reusable samples and the
browser/manual verification matrix. Editing is deliberately deferred: revise the JSON through the
AI workflow, then reload and review the resulting digest.

## Development setup

Marvin Pilot requires Python 3.11 or newer.

After completing the source installation above, add the development dependencies:

```console
.venv/bin/python -m pip install -e '.[dev]'
```

On Windows, use `.venv\Scripts\python.exe -m pip install -e ".[dev]"`.

The planned public distribution name is `amazing-marvin-pilot`; PyPI publishing has not begun.

## Credential modes

The onboarding above uses the recommended keyring mode. `marvin-pilot config` also provides a
guided setup, while the subcommands make each choice explicit:

```console
marvin-pilot config
```

The recommended mode uses the native OS credential service: macOS Keychain, Windows Credential
Locker, or a supported Linux Secret Service/KWallet backend. The ordinary TOML config stores no
token.

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

# Optional real-Chromium visualizer suite
.venv/bin/python -m playwright install chromium
MARVIN_PILOT_BROWSER_TESTS=1 .venv/bin/python -m pytest -m browser tests/browser
```

In PowerShell, set the browser-suite flag with
`$env:MARVIN_PILOT_BROWSER_TESTS = "1"`. CI runs this suite in a dedicated Chromium job and also
checks that the wheel and source distribution contain every offline visualizer asset.

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
Marvin. The Amazing Marvin mascot remains the property of Amazing Marvin and is included only to
identify the product this tool works with.
