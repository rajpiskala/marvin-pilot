# Marvin Pilot

**Your AI plans. You approve. Marvin Pilot applies.**

Marvin Pilot is a local approval CLI for [Amazing Marvin](https://amazingmarvin.com/) that lets an AI reorganize your tasks and projects **without giving it your full-access API token**.

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
> **Marvin Pilot is pre-alpha.** The task lifecycle and a 200-operation scale run were verified on 2026-08-08, project CRUD plus historical completion on 2026-08-11, ordered subtask CRUD/consolidation/revert on 2026-08-14, and recurrence-series plus explicit-occurrence CRUD/revert on 2026-08-15, against a dedicated development account. The client has not yet seen enough real-world account shapes and upstream conditions for production use. Back up Marvin and evaluate it with non-critical data first.

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
marvin-pilot doctor
```

Enter `FULL_ACCESS_TOKEN` at the hidden prompt.

`config show` deliberately never prints the credential.

`doctor` makes one read-only full-access `/api/me` request to verify that the credential can be
loaded, Marvin is reachable, and Marvin accepts the token. Its colored report shows the connected
account email and user ID, request URL, exact HTTP status and reason, response time, and safety
result. A redirected or captured non-color terminal gets the same report as plain text. It does not
read a task, write a receipt, or change any Marvin data. Authentication failures and network/API
failures use distinct exit codes and actionable messages without printing the token; responses such
as 404, 429, and 500 retain their exact HTTP status in the failure report.

Two stricter modes are also available:

```console
marvin-pilot config
```

* `prompt` — enter the token at every live command
* `file` — read it from a carefully permissioned local file

Marvin Pilot intentionally provides no token environment variable, `--full-access-key VALUE`, or
fully non-interactive mutation mode. `apply --yes` is a reviewed power-user shortcut: it skips only
the final approval question, after live preflight, and still refuses to run without an interactive
controlling terminal.

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

Marvin Pilot shows a compact preflight progress bar with the current operation ID, then presents a
color-enhanced terminal review. Every operation has a textual `CREATE`, `UPDATE`, `COMPLETE`, or
`TRASH` label; a distinct task, project, recurring-series, or occurrence marker; target and
operation IDs; Marvin hierarchy; and an exact Now/After field table. Marvin emoji and `#RRGGBB`
metadata are used when supplied by the plan or live document. Color chips automatically choose
black or white text for WCAG contrast, and textual labels plus encoding-safe fallbacks keep all
meaning available in monochrome, redirected, and legacy Windows terminals.

By default, `apply` asks once for confirmation before making changes. If you have already reviewed
the complete plan and want the faster daily workflow, use:

```console
marvin-pilot apply plans/first-plan.json --yes
# short form
marvin-pilot apply plans/first-plan.json -y
```

`--yes` never skips parsing, limits, live preflight, concurrency checks, receipt journaling, API
verification, or the interactive-terminal requirement. It only skips the final `[y/N]` keystroke.
`revert` deliberately continues to require its explicit prompt. Apply and revert use a separate
progress bar, so their elapsed time and ETA cover only that phase instead of including the
preflight wait.

The default client starts requests at least 750 ms apart. A normal operation needs one live
concurrency recheck plus one mutation whose returned document is verified directly. If Marvin
returns a partial or unexpected response, Pilot safely falls back to a separate read-back. Large
plans therefore remain sequential and rate-aware without paying for an unconditional third request
per operation.

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

## Historical project context from a backup

The limited Marvin API can return open project children, but it has no efficient endpoint for every
completed descendant of a project. For audits and historical reclustering, export a current Marvin
backup and let Pilot build compact context locally:

```console
marvin-pilot context project "Project Atlas" \
  --backup MarvinBackup.json.lzma \
  --output dev/project-atlas-context.json
```

The positional value may be an exact category/project title or document ID. When a title appears
more than once, Pilot reports each hierarchy path and asks for the exact ID. Both uncompressed
`.json` and Marvin's compressed `.json.lzma` exports are supported, including Marvin's CESU-8 emoji
encoding.

The context includes every nested category, project, ordinary task, generated recurring occurrence,
and recurrence definition beneath the selected root. Completed items, original completion dates,
notes, ordering, recurrence identity, and ordered subtasks are retained; `updatedAt` remains numeric
so a later plan builder can establish exact live locks. Trash subtrees are omitted unless
`--include-trash` is supplied. The command performs no Marvin API calls and needs no credential.

> [!CAUTION]
> A backup and its derived context contain private task data. Keep both outside Git or under the
> ignored `dev/`/`plans/` directories. Pilot never embeds the backup filename or path in context,
> but the resulting JSON intentionally contains the selected tasks and notes. A backup is a
> snapshot, not current authority; `validate --live`/`apply` must still fetch each selected target
> and reject stale titles, parents, completion timestamps, or `updatedAt` values before writing.

This deliberately avoids a persistent cache and invalidation service. For a fresh historical audit,
provide a fresh backup; Pilot parses it directly and leaves no expanded copy behind.

## Core commands

| Command                               | What it does                               |
| ------------------------------------- | ------------------------------------------ |
| `marvin-pilot doctor`                 | Show account, HTTP, and full-token health  |
| `marvin-pilot context project …`      | Extract full project history from a backup |
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

The default **Preview** renders Marvin-like **Now** and **After (preview)** hierarchies. Inbox, categories, projects, tasks, recurrence definitions, occurrences, and subtasks have distinct Marvin-like visual treatment; projects visibly support create, rename, move, schedule, complete, and Trash transitions. A generated occurrence keeps its task circle and shows a compact recurrence-loop icon on the right, while a recurrence definition omits the task circle and uses the loop icon as its type marker. Tooltips and item details spell out whether a change affects one generated occurrence or the series template. Completed After cards show the exact marked-done timestamp using the reviewer's browser locale, time zone, and 12/24-hour convention. Moved items render under their truthful parent on each side and cross-highlight their counterpart, while shared hierarchy disclosure stays synchronized. Click any row to pin its two states in a sticky comparison tray; moved-item Previous/Next and Jump controls avoid hunting for a far-away destination. Deep hierarchies scroll horizontally within each pane, and single-state views provide the full content width.

Use **Day sections: Show/Hide** to layer explicit Today-list groupings over the hierarchy. Day sections such as Waiting or Main are visually distinct from categories and projects. Switch to **Changes** for an aligned operation diff grouped by typed After location, Now location, plan order, or supplied Today section. Search and the Moved filter keep large cleanups navigable. Action totals filter either mode, and item details expose the reason, identifiers, exact field diff, hierarchy path, supplied day-section context, and structured subtask changes.

It does not load a Marvin credential, call the Marvin API, persist task data in browser storage, or provide mutation controls. The selected plan is passed through the same strict validator used by `apply`.

Press `Ctrl+C` in the launching terminal to stop it.

Typed `display.beforePath` and `display.afterPath` metadata supplies offline ancestry without affecting apply. Empty paths mean a known Marvin root; omitted paths render under **Location not supplied** instead of being guessed. Completion is the safe exception: when a known `beforePath` is present and `afterPath` is omitted, the completed After state inherits that unchanged ancestry. Explicit `afterPath: null` remains unknown. Optional path-node and target order values reproduce sibling ordering, while legacy `beforeSection`/`afterSection` values remain a visibly inferred fallback.

An update or Trash operation on an already-completed task may provide its original RFC 3339 timestamp as `display.existingCompletedAt`. Preview then renders the task as completed, with the localized completion timestamp, in every state where it exists. Live preflight requires this exact metadata before updating a completed task and verifies it against Marvin's `doneAt`; the review-only field never becomes a setter. Reparenting therefore changes only `parentId` and preserves both `done` and `doneAt` through apply and revert.

The browser suite includes synthetic hierarchy, lifecycle, movement, and subtask cases. Maintainers
can point `MARVIN_PILOT_PRIVATE_PLAN_DIR` at an ignored local regression corpus containing
`01.json` through `04.json`; private fixtures must never be committed.

## Plans and recovery

Plans are versioned, closed-schema JSON documents containing stable `operationId` values and typed `create`, `update`, `complete`, or `trash` operations. Targets can be tasks, projects, entire recurring-task series, or explicitly identified generated occurrences. A recurrence series cannot itself be completed; complete one generated occurrence or Trash the series instead.

Generate the authoritative schema and example directly from the installed CLI:

```console
marvin-pilot schema --output change-plan.schema.json
marvin-pilot example --output plan.json
marvin-pilot help plan-format
```

V1 supports common task and project fields including titles, parents/categories, dates and scheduling, labels, estimates, notes, ranks, sections, priorities, backburner state, review dates, and snooze values. Tasks additionally support star priority, `masterRank`, dependencies, and ordered embedded `subtasks`. JSON `null` clears a supported value.

Each subtask has a stable `id`, exact `title`, and `done` state; array order becomes native Marvin rank. Retained subtask records are merged by ID so unknown native metadata survives. Omission removes a prior subtask and `null` clears the checklist. A new subtask may include review-only `sourceTask: {id, title}` when consolidating a loose task, but only with a later dependent `trash` operation. Live preflight refuses stale, coupled, completed, or metadata-rich sources that cannot be represented losslessly, and receipts restore the original embedded map exactly.

Project creates write native `Categories` documents with `type: "project"`; updates can rename, move, or edit allowlisted fields; completion records an explicit historical RFC 3339 timestamp; and Trash uses the same receipt-backed deletion as tasks. Planned project ancestry is checked before writes, including parents created earlier in the same plan and cycle prevention.

Task completion backdates both Marvin's `doneAt` value and the completion field-update timestamps
that Marvin uses to place the item in completion-day views. Project completion similarly backdates
`doneDate` and its completion field-update timestamps. `updatedAt` still records the actual apply
time, preserving an honest concurrency lock. This prevents an item marked done for an earlier date
from appearing under **Completed Today** merely because the plan was applied today.

Completed tasks remain ordinary task documents addressable by exact ID. Pilot can rename or reparent them without reopening them, but the plan must include the verified `display.existingCompletedAt` timestamp so the historical state is visible during review. This supports backup-assisted historical reorganization while retaining a fresh live-state and concurrency check before every write.

### Recurring tasks

Use `target.type: "recurringTask"` to create, update, or Trash an entire recurring-task series. Series creates require an explicit `cadence`; supported cadence types are `daily`, `weekly`, `monthly`, `n per week`, `repeat`, `repeat week`, `repeat month`, `repeat year`, `echo`, `onOff`, and `custom`. The cadence includes an explicit `startDate` and may include `endDate`. Series fields also include title, parent, labels, estimate, note, ordered subtask templates, star/frog markers, and relative due-in days. Updating a series changes the template used for future generated occurrences; it does not silently rewrite already-generated tasks.

Marvin, rather than Pilot, generates task occurrences from that template. A live remote-create check confirmed that a newly written same-day template synced into the browser but did not immediately backfill a generated occurrence during reload. Do not assume a same-day occurrence exists until Marvin has generated it; Pilot does not synthesize one as a hidden side effect.

An operation aimed at one generated occurrence remains `target.type: "task"` and must include:

```json
{
  "recurrence": {
    "scope": "occurrence",
    "seriesId": "the-series-id",
    "seriesTitle": "Expected series title",
    "scheduledDate": "2026-08-15"
  }
}
```

Live preflight verifies all four facts against both the occurrence and its current series before allowing an update, completion, or Trash. It rejects a generated recurring task when this declaration is missing, preventing a plan from accidentally treating one occurrence as an ordinary task or an entire series. Completion-coupled `echo` occurrences remain blocked because completing or deleting one creates the next occurrence as a side effect.

Deleting an explicit generated occurrence tombstones that exact task document. Pilot deliberately does not edit the series template's `deletedDates`: the explicit occurrence ID is already deletion-protected by its CouchDB tombstone, while changing the template can suppress a different retained occurrence when an old task has rolled forward to today's `day`. Deleting an entire series removes only its recurrence-template document; already-generated task occurrences remain separate documents unless the plan explicitly includes them.

Series subtasks preserve declared order and are copied by Marvin into future occurrences. Generated occurrences use the ordinary task-subtask model, including stable IDs and done state. Receipts record recurrence scope, and conflict-aware revert supports both series edits and explicit occurrence edits.

One upstream distinction matters for audits: a task completed through the full-access document path receives native completion fields, but live testing found that the limited `/doneItems` endpoint does not index that backdated mutation. Project completion dates remain directly readable as `doneDate`. Use the receipt or full document rather than `/doneItems` as the sole oracle for Pilot-applied historical completion.

### Pilot-managed Trash and recovery

Marvin's native Trash is client-side: the app saves a copy in browser-local storage and then deletes the synced document. The public API cannot add an item to that local Trash. Pilot's `trash` action therefore uses `/doc/delete`, but only after a pending receipt containing the complete original document has been durably written. Successful post-write verification requires the exact document ID to be absent. This fixes the former `deletedAt`-only behavior, which left pseudo-trashed tasks live and visible in Today.

Pilot recovery is receipt-backed rather than Marvin-native. `marvin-pilot revert RECEIPT.json` recreates the same ID from the stored document after removing stale CouchDB `_rev`/`_deleted` fields. Reverting a Pilot `create` likewise deletes the created document instead of leaving a hidden live record. Neither deletion appears in Marvin's native Trash UI.

Every live apply writes audit state before the first mutation and records field-scoped before/after data and per-operation outcomes. Trash receipts include the full original task, project, or recurrence-template document—including notes and other personal content—because that snapshot is the recovery source. Keep the history directory private and do not commit or share receipts casually.

Revert is conflict-aware: updates and completions restore only fields changed by the original apply and preserve unrelated later edits. A trashed document is restored only while its ID remains absent. A created document is deleted on revert only when its complete post-create snapshot is unchanged.

Receipts include a SHA-256 integrity hash to detect accidental modification. The hash is **not** a cryptographic signature and does not make history tamper-proof.

## Safety model

The intended boundary is simple:

**AI:** read tasks, draft plans, validate plans, explain plans.

**Human:** review plans, run `apply`, run `revert`.

`apply` and `revert` require an interactive controlling terminal and default to **No**. A human may
opt into `apply --yes` after reviewing a plan; it skips the final keystroke but not the terminal
requirement or any safety check. Revert has no equivalent bypass.

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

Live contract testing has covered the v1 task and project field set, project CRUD, historical completion (including Marvin's completion-index timestamps), ordered subtask create/read/update/delete/reorder/complete/reopen/consolidation, recurrence-series create/read/update/Trash/restore/revert, explicit generated-occurrence update/complete/Trash/revert, create/update/schedule/unschedule/Trash/restore/revert workflows, and a 200-operation scale run against a dedicated development account. Recurrence scope also has unit, in-memory integration, and browser-visualizer coverage; its disposable live-account contract is described in `contract-tests/README.md`.

See [`contract-tests/README.md`](contract-tests/README.md) before running live contract cases.

Developer investigation notes, live-account reports, and private visual-regression corpora belong
under the ignored `dev/` directory. Durable setup, safety, and supported behavior must be documented
in this README or the contract-test guide so a checkout is safe to share by default.

## Project status

Marvin Pilot is currently **pre-alpha**. The planned PyPI distribution name is `amazing-marvin-pilot`; public PyPI publishing has not begun.

Marvin Pilot is an independent community project and is not affiliated with or endorsed by Amazing Marvin.
