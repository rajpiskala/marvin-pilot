# Marvin Pilot

**Your AI plans. You approve. Marvin Pilot applies.**

Marvin Pilot is a local approval CLI for [Amazing Marvin](https://amazingmarvin.com/) that lets an AI reorganize your tasks **without giving it your full-access API token**.

The AI reads your workload through the limited-access [Amazing Marvin MCP](https://github.com/bgheneti/Amazing-Marvin-MCP) and writes a strict JSON change plan. You review the diff, then run the mutation yourself.

```text
Amazing Marvin MCP       AI             You              Marvin Pilot
  limited token   ->   plan.json   ->  review  ->  apply with full token
                                                       |
                                                       +-> receipt
                                                       +-> revert
```

Marvin Pilot validates the plan against live state, asks for confirmation, applies operations one at a time, verifies the result, and writes an integrity-checked receipt that can be fully or selectively reverted.

> [!WARNING]
> **Marvin Pilot is pre-alpha.** The v1 lifecycle and a 200-operation scale run have been tested against a dedicated development account, but the client has not yet seen enough real-world account shapes and upstream conditions for production use. Back up Marvin and evaluate it with non-critical data first.

## Why Marvin Pilot?

Giving an AI Marvin's `FULL_ACCESS_TOKEN` would let it make arbitrary changes directly.

Marvin Pilot keeps that credential on the human side of the workflow.

|                     | AI / MCP                                   | Marvin Pilot                     |
| ------------------- | ------------------------------------------ | -------------------------------- |
| Credential          | `API_TOKEN`                                | `FULL_ACCESS_TOKEN`              |
| Purpose             | Read workload through Marvin's limited API | Apply reviewed changes           |
| Can run unattended? | Yes                                        | No                               |
| Mutation approval   | —                                          | Interactive `[y/N]`              |
| Recovery            | —                                          | Receipts + conflict-aware revert |

The full-access token is never placed in a plan, MCP configuration, AI prompt, command-line argument, or environment variable.

## Quick start

Marvin Pilot requires **Python 3.11+** and currently installs from source.

```console
git clone https://github.com/rajpiskala/marvin-pilot.git
cd marvin-pilot
python -m venv .venv
```

Activate the environment and install:

```console
# macOS / Linux
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Check the CLI:

```console
marvin-pilot --help
```

### 1. Get both Marvin credentials

In Amazing Marvin, open **Settings → API** and enable API access.

You need:

* `API_TOKEN` — the limited-access credential used by the MCP
* `FULL_ACCESS_TOKEN` — the powerful credential used only by Marvin Pilot

Both should belong to the same Marvin account.

See the [Amazing Marvin API credential documentation](https://github.com/amazingmarvin/MarvinAPI/wiki/Marvin-API#credentials).

### 2. Give the MCP only the limited token

Marvin Pilot includes a launcher that stores the MCP's `API_TOKEN` in your operating system's credential store instead of your AI client's configuration.

<details>
<summary><strong>macOS / Linux</strong></summary>

```console
python -m venv .tools/amazing-marvin-mcp
.tools/amazing-marvin-mcp/bin/python -m pip install --upgrade pip
.tools/amazing-marvin-mcp/bin/python -m pip install amazing-marvin-mcp keyring
.tools/amazing-marvin-mcp/bin/python tools/amazing_marvin_mcp_launcher.py --store-key
```

Enter the limited `API_TOKEN` at the hidden prompt.

For Codex:

```console
codex mcp add amazing-marvin -- \
  "$PWD/.tools/amazing-marvin-mcp/bin/python" \
  "$PWD/tools/amazing_marvin_mcp_launcher.py"
```

</details>

<details>
<summary><strong>Windows PowerShell</strong></summary>

```powershell
python -m venv .tools\amazing-marvin-mcp
.\.tools\amazing-marvin-mcp\Scripts\python.exe -m pip install --upgrade pip
.\.tools\amazing-marvin-mcp\Scripts\python.exe -m pip install amazing-marvin-mcp keyring
.\.tools\amazing-marvin-mcp\Scripts\python.exe tools\amazing_marvin_mcp_launcher.py --store-key
```

Enter the limited `API_TOKEN` at the hidden prompt.

For Codex:

```powershell
$mcpPython = (Resolve-Path .\.tools\amazing-marvin-mcp\Scripts\python.exe).Path
$mcpLauncher = (Resolve-Path .\tools\amazing_marvin_mcp_launcher.py).Path
codex mcp add amazing-marvin -- $mcpPython $mcpLauncher
```

</details>

For another MCP client, use the MCP environment's Python executable as the STDIO command and `tools/amazing_marvin_mcp_launcher.py` as its argument.

**Do not put `FULL_ACCESS_TOKEN` in the MCP configuration.**

Restart your AI client and verify that the MCP is connected to the intended Marvin account before continuing.

### 3. Give Marvin Pilot the full-access token

The recommended credential mode uses your operating system's native credential store:

```console
marvin-pilot config set-credential-mode keyring
marvin-pilot config set-full-access-token
marvin-pilot config show
```

Enter `FULL_ACCESS_TOKEN` at the hidden prompt.

`config show` deliberately never prints the credential.

Two stricter modes are also available:

```console
marvin-pilot config
```

* `prompt` — enter the token at every live command
* `file` — read it from a carefully permissioned local file

Marvin Pilot intentionally provides no token environment variable, `--full-access-key VALUE`, `--yes`, or non-interactive mutation mode.

## Your first plan

Start small.

Ask your AI:

> Read my Marvin tasks through the Amazing Marvin MCP. Draft a Marvin Pilot change plan with at most 10 low-risk operations and save it under `plans/`. Do not use MCP mutation tools and do not run `marvin-pilot apply` or `marvin-pilot revert`; I will review and run those myself.

`plans/` is ignored by this repository because plans can contain private task data.

Validate and inspect the plan:

```console
marvin-pilot validate plans/first-plan.json
marvin-pilot describe plans/first-plan.json
marvin-pilot visualize plans/first-plan.json
```

When the plan looks correct:

```console
marvin-pilot apply plans/first-plan.json
```

Marvin Pilot performs a live preflight, shows the operations, and asks once for confirmation before making changes.

It then prints the path to an audit receipt.

If you need to undo the result:

```console
marvin-pilot revert path/to/applied-receipt.json
```

Or revert selected operations:

```console
marvin-pilot revert path/to/applied-receipt.json \
  --only operation-one \
  --only operation-two
```

For large reorganizations, prefer reviewed batches over one enormous plan.

## Core commands

| Command                               | What it does                               |
| ------------------------------------- | ------------------------------------------ |
| `marvin-pilot validate PLAN`          | Strictly validate a plan offline           |
| `marvin-pilot describe PLAN`          | Print a human-readable description         |
| `marvin-pilot visualize PLAN`         | Open the local visual diff                 |
| `marvin-pilot apply PLAN`             | Preflight, confirm, apply, verify, receipt |
| `marvin-pilot revert RECEIPT`         | Revert an applied receipt                  |
| `marvin-pilot history list`           | List audit receipts                        |
| `marvin-pilot history show latest`    | Inspect the newest receipt                 |
| `marvin-pilot history verify RECEIPT` | Verify receipt integrity                   |
| `marvin-pilot schema --output FILE`   | Export the plan schema                     |
| `marvin-pilot example --output FILE`  | Generate an example plan                   |
| `marvin-pilot help plan-format`       | Explain the v1 plan format                 |

## Visual review

`marvin-pilot visualize` opens a credential-free local browser view of the proposed changes.

```console
marvin-pilot visualize plan.json
```

The visualizer shows **Now** and **After** side by side, including creates, updates, moves, and Trash operations.

It does not load a Marvin credential, call the Marvin API, persist task data in browser storage, or provide mutation controls. The selected plan is passed through the same strict validator used by `apply`.

Press `Ctrl+C` in the launching terminal to stop it.

See [`docs/visualizer-test-matrix.md`](docs/visualizer-test-matrix.md) for reusable samples and browser verification coverage.

## Plans and recovery

Plans are versioned, closed-schema JSON documents containing stable `operationId` values and typed `create`, `update`, or `trash` operations.

Generate the authoritative schema and example directly from the installed CLI:

```console
marvin-pilot schema --output change-plan.schema.json
marvin-pilot example --output plan.json
marvin-pilot help plan-format
```

V1 supports common Marvin task fields including titles, categories, dates and scheduling, labels, estimates, notes, ranks, sections, priorities, backburner state, review dates, snooze values, and dependencies.

Permanent deletion is intentionally not implemented. `trash` uses Marvin's reversible Trash behavior.

Every live apply writes audit state before the first mutation and records field-scoped before/after data and per-operation outcomes.

Revert is conflict-aware: it restores only fields changed by the original apply, preserves unrelated later edits, and refuses to overwrite a touched field that has changed since.

Receipts include a SHA-256 integrity hash to detect accidental modification. The hash is **not** a cryptographic signature and does not make history tamper-proof.

## Safety model

The intended boundary is simple:

**AI:** read tasks, draft plans, validate plans, explain plans.

**Human:** review plans, run `apply`, run `revert`.

`apply` and `revert` require an interactive controlling terminal and default to **No**.

The full-access credential is retrieved only when a live command needs it and is never written into plans or receipts.

This is a workflow and credential boundary, not an operating-system sandbox. Software running as the same user may still be able to invoke the CLI or interact with the system credential service. Use `prompt` or carefully permissioned `file` mode if that threat matters to you.

## Development

Install development dependencies:

```console
# macOS / Linux
.venv/bin/python -m pip install -e '.[dev]'

# Windows
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Run the main checks:

```console
python -m pytest --cov=marvin_pilot
python -m ruff format --check src tests
python -m ruff check src tests
```

The optional Chromium visualizer suite uses Playwright:

```console
python -m playwright install chromium
MARVIN_PILOT_BROWSER_TESTS=1 python -m pytest -m browser tests/browser
```

Live contract testing has covered the v1 field set, create/update/schedule/unschedule/Trash/restore/revert workflows, and a 200-operation scale run against a dedicated development account.

See [`docs/live-contract-test-report.md`](docs/live-contract-test-report.md) for sanitized results and [`contract-tests/README.md`](contract-tests/README.md) before running live contract cases.

The deeper API research, threat model, design decisions, and rollout gates live in [`Implementation-Plan.md`](Implementation-Plan.md).

## Project status

Marvin Pilot is currently **pre-alpha**. The planned PyPI distribution name is `amazing-marvin-pilot`; public PyPI publishing has not begun.

Marvin Pilot is an independent community project and is not affiliated with or endorsed by Amazing Marvin.
