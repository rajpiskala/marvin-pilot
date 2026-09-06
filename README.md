# Marvin Pilot

**Your AI plans. You approve—or preauthorize a small limit. Marvin Pilot applies.**

Marvin Pilot is a local approval CLI for [Amazing Marvin](https://amazingmarvin.com/) that lets an AI reorganize your tasks and projects **without giving it your full-access API token**.

The AI can read current work through the limited-access [Amazing Marvin MCP](https://github.com/bgheneti/Amazing-Marvin-MCP), ask Pilot for verified live Today context, or analyze a local Marvin backup. It may write a full strict plan or a compact intent draft that Pilot expands into the locked review artifact. You can review and run the mutation yourself, or explicitly enable account-pinned unattended apply for small plans.

```text
MCP / Pilot context       AI                 Marvin Pilot                  Marvin
 read-only discovery  -> draft/plan -> prepare + validate + review -> apply
                                              |                         |
                                              +-> exact plan            +-> receipt -> revert
```

Marvin Pilot validates the plan against live state, enforces configured limits, applies operations one at a time, verifies the result, and writes an integrity-checked receipt that can be fully or selectively reverted. Normal apply asks for confirmation; bounded unattended apply uses a policy that a human enabled in advance.

> [!WARNING]
> **Marvin Pilot is alpha software.** Task, project, category, subtask, recurrence, historical-completion, dependency-chain, and receipt-backed recovery paths have automated coverage and extensive development-account use. The client still cannot preserve every coupled Marvin feature. Back up Marvin and evaluate it with non-critical data first.

## Why Marvin Pilot?

Giving an AI Marvin's `FULL_ACCESS_TOKEN` would let it make arbitrary changes directly.

Marvin Pilot keeps that credential on the human side of the workflow.

|                     | AI / MCP                                   | Marvin Pilot                     |
| ------------------- | ------------------------------------------ | -------------------------------- |
| Credential          | `API_TOKEN`                                | `FULL_ACCESS_TOKEN`              |
| Purpose             | Read workload through Marvin's limited API | Apply reviewed or bounded changes |
| Can run unattended? | Yes                                        | Only within an enabled local cap  |
| Mutation approval   | —                                          | Interactive or preauthorized cap  |
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

Marvin Pilot intentionally provides no token environment variable or `--full-access-key VALUE`.
`apply --yes` is a reviewed power-user shortcut: it skips only the final approval question, after
live preflight, and still refuses to run without an interactive controlling terminal. Separately,
a human can opt into tightly bounded unattended apply as described below.

## Your first plan

Start small.

Ask your AI:

> Read my Marvin tasks through the Amazing Marvin MCP. Draft a Marvin Pilot change plan with at most 10 low-risk operations and save it under `plans/`. Do not use MCP mutation tools and do not run `marvin-pilot apply` or `marvin-pilot revert`; I will review and run those myself.

`plans/` is ignored by this repository because plans can contain private task data.

For new plans, include `expectedAccount: {"userId":"…","email":"…"}` from a verified context
or `doctor` result. Live validation checks the account before reading target documents, and apply
checks it again immediately before any write. Bounded unattended apply requires this binding.

For small or repetitive changes, the AI can author a compact closed-schema draft and let Pilot
fill titles, before-state, concurrency locks, create UUIDs, and hierarchy metadata:

```console
marvin-pilot context search "Project title" --backup MarvinBackup.json.lzma
marvin-pilot prepare draft.json --backup MarvinBackup.json.lzma --output plan.json
# Add --live to overlay current target documents on the backup before compiling.
```

`prepare` never chooses desired titles, dates, parents, actions, or completion times. Existing
targets can be exact IDs or a title that uniquely matches after conservative Unicode/emoji
normalization in the backup. Ambiguous matches stop and list IDs. To refresh only generated locks
and paths in an already reviewed plan, use `prepare PLAN --live --rebase-live -o NEW-PLAN`; semantic
drift stops the rebase and requires review.

Validate and inspect the plan:

```console
marvin-pilot validate plans/first-plan.json
marvin-pilot describe plans/first-plan.json
marvin-pilot describe plans/first-plan.json --format markdown --output plans/first-plan.md
marvin-pilot visualize plans/first-plan.json
```

Before handing the plan to the human, an AI assistant may run the read-only live validator:

```console
marvin-pilot validate plans/first-plan.json --live
```

Live validation fetches each unique referenced Marvin document once, reports every error and
non-blocking hint warning in one run, and returns nonzero when any error is present. It never calls
a mutation endpoint. Use `--json` for stable diagnostics containing the plan index, operation ID,
target ID, check name, expected value, and live value. Progress remains on stderr, leaving stdout
machine-readable.
The summary reports unique documents/metadata collections checked and elapsed time. Within a plan,
and across projected phases in a plan set, reads are cached by stable ID; apply still performs an
uncached concurrency recheck immediately before each write.

For a large plan, narrow only the advisory live scan:

```console
# Entries 190 through the end (1-based)
marvin-pilot validate plan.json --live --from-index 190

# This named operation through the end
marvin-pilot validate plan.json --live --from-operation move-task-a

# Every operation targeting one or more exact Marvin document IDs
marvin-pilot validate plan.json --live --target DOC_ID
```

Selectors avoid unrelated reads but still fetch required parents, dependencies, recurrence
templates, and source tasks. `--fail-fast` restores one-error diagnostic behavior. `apply` never
accepts these selectors: immediately before approval it revalidates the complete plan and reports
all violations, because an earlier successful diagnostic scan is not current write authority.

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

By default, `apply` requires an explicit `y` or `n` before making changes. Empty or unrecognized
input explains the accepted answers and re-prompts instead of silently choosing a default. An
explicit decline confirms that no Marvin data changed. If you have already reviewed the complete
plan and want the faster daily workflow, use:

```console
marvin-pilot apply plans/first-plan.json --yes
# short form
marvin-pilot apply plans/first-plan.json -y
```

`--yes` never skips parsing, limits, live preflight, concurrency checks, receipt journaling, API
verification, or the interactive-terminal requirement. It only skips the final explicit `y`/`n`
decision.
`revert` deliberately continues to require its explicit prompt. Apply and revert use a separate
progress bar, so their elapsed time and ETA cover only that phase instead of including the
preflight wait.

### Bounded unattended apply

For routine changes such as creating one project and four tasks, a human can authorize a local
maximum once:

```console
marvin-pilot config unattended enable --max-impact 10
```

Pilot verifies the currently connected account, shows its email and user ID, explains the retained
safety checks, and asks the human for confirmation. The saved non-secret policy pins unattended
apply to that exact account ID. Swapping credentials to another account makes unattended apply fail
before any plan document is read or changed.

An AI acting on an explicit user request can then use a plan file or pipe the same strict JSON plan
through stdin:

```console
marvin-pilot apply --unattended plans/small-change.json
Get-Content plans/small-change.json -Raw | marvin-pilot apply --unattended -
```

Unattended apply skips only the controlling-terminal requirement and approval prompt. Schema and
semantic validation, the general operation ceiling, full live preflight, account verification,
strict concurrency, pending receipt creation, per-operation rechecks, mutation verification, and
failure journaling remain mandatory.

Impact counts each plan operation plus every embedded subtask involved in a task create, subtask
update, or task Trash. For example, creating one project and four plain tasks has impact 5; creating
one task with four subtasks also has impact 5. This prevents a one-operation task from concealing a
very large checklist mutation. The command cannot raise the configured maximum.

Project/category Trash and every recurring-series create, update, or Trash remain interactive-only because
one container or template operation can have a much larger apparent or future effect. Operations
on an explicitly identified generated recurrence occurrence are ordinary task operations and can
qualify. Revert always remains interactive.

Disable or inspect the policy with:

```console
marvin-pilot config unattended show
marvin-pilot config unattended disable
```

When unattended apply is disabled, an eligible reviewed plan with impact 10 or less prints a short
setup tip. Nothing is enabled automatically.

The plan schema allows at most 500 operations, and the default general runtime ceiling is also 500.
Set a lower ceiling for both reviewed apply and revert with:

```console
marvin-pilot config set-max-operations 100
```

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

Before reusing old artifacts, inspect their receipt-backed local state:

```console
marvin-pilot history status plans/ --recursive
marvin-pilot history status cleanup.plan-set.json --json
marvin-pilot history audit path/to/applied-receipt.json --live
marvin-pilot history audit path/to/old-receipt.json --live --repair-plan completion-repair.json
```

Status distinguishes never applied, applied, partial/ambiguous, fully reverted, selectively
reverted, and changed artifacts without calling Marvin. `history audit --live` then compares each
verified receipt post-state with current full documents, separating a still-matching result from a
later edit, missing document, or unexpectedly present deleted target. It notes that Marvin UI
caches can lag behind the document API. For task and project completions, the audit separately
classifies the full document's completion timestamp and completion-history day. If an older task
receipt proves that the timestamp still matches but `day` is missing or wrong, `--repair-plan`
writes a normal update that changes only that history day. If an older project receipt proves that
`doneAt` is missing while `doneDate` still matches the reviewed timestamp, it writes a guarded
`repairHistory` completion that restores `doneAt` and aligns `day`. Every repair plan is
account-pinned and concurrency-locked; the command never applies it or overwrites an existing
project timestamp.

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

Backup reads use a private content-addressed cache of parsed documents. The key is the SHA-256 of
the backup bytes, so a changed backup cannot reuse stale data; Pilot never stores the source path.
Only five entries are retained by least-recent use. Inspect or clear it with:

```console
marvin-pilot context backup-info MarvinBackup.json.lzma
marvin-pilot context cache-clear
```

The cache removes repeated decompression/parsing, not freshness checks. Supply a new backup when
you need newer state. Search and bounded extraction avoid emitting an entire private account:

```console
marvin-pilot context search "Project Atlas" --backup MarvinBackup.json.lzma
marvin-pilot context project "Project Atlas" --backup MarvinBackup.json.lzma --summary
marvin-pilot context project PROJECT_ID --backup MarvinBackup.json.lzma --max-depth 2 --state open
marvin-pilot context project PROJECT_ID --backup MarvinBackup.json.lzma --state completed --since 2026-06-01
```

Amazing Marvin's `/categories` endpoint returns category documents, not the complete project tree.
Pilot builds the full category/project hierarchy from one backup instead of probing projects one at
a time. `context project` includes open and completed descendants, which supports historical
reclustering of hundreds of completed tasks.

A backup cannot reproduce Marvin's Today view: Today also includes rollover and strategy/automatic
scheduling. Use the authoritative read-only endpoint instead:

```console
marvin-pilot context today --live --date 2026-09-04 --timezone America/Los_Angeles
```

That response includes exact IDs, full-document `updatedAt`, ancestor paths, recurrence template
identity, and a conservative reason (`scheduled-date`, `rollover`, or
`strategy-or-auto-schedule`). `context scheduled-day DATE --backup …` is intentionally narrower:
it returns documents whose stored day equals DATE and explicitly does not claim to be Today.

## Core commands

| Command                               | What it does                               |
| ------------------------------------- | ------------------------------------------ |
| `marvin-pilot doctor`                 | Show account, HTTP, and full-token health  |
| `marvin-pilot context project …`      | Extract bounded project history from backup |
| `marvin-pilot context search …`       | Resolve normalized candidates and exact IDs |
| `marvin-pilot context today --live`   | Get authoritative Today context and ancestry |
| `marvin-pilot prepare INPUT …`        | Compile intent or safely rebase plan metadata |
| `marvin-pilot validate PLAN`          | Strictly validate a plan offline           |
| `marvin-pilot validate PLAN --live`   | Collect read-only live diagnostics         |
| `marvin-pilot describe PLAN`          | Describe as text, Markdown, or JSON         |
| `marvin-pilot visualize PLAN`         | Open the local visual diff and history     |
| `marvin-pilot apply PLAN`             | Preflight, confirm, apply, verify, receipt |
| `marvin-pilot revert RECEIPT`         | Revert an applied receipt                  |
| `marvin-pilot history list`           | List audit receipts                        |
| `marvin-pilot history status SOURCE`  | Report local apply/revert status            |
| `marvin-pilot history audit R --live` | Audit current documents; optionally draft history repair |
| `marvin-pilot history show latest`    | Inspect the newest receipt                 |
| `marvin-pilot history verify RECEIPT` | Verify receipt integrity                   |
| `marvin-pilot schema --output FILE`   | Export plan/draft/plan-set JSON Schema      |
| `marvin-pilot example --output FILE`  | Generate an example plan                   |
| `marvin-pilot help plan-format`       | Explain the v1 plan format                 |

## Visual review

`marvin-pilot visualize` opens a credential-free local browser view of the proposed changes.

```console
marvin-pilot visualize plan.json
```

If an AI-authored plan contains parent IDs but omits the optional typed display paths, supply a
fresh local Marvin backup to reconstruct the hierarchy:

```console
marvin-pilot visualize plan.json --backup MarvinBackup.json.lzma
```

For a dependent phase, project each earlier plan locally in dependency order. This resolves
projects created or renamed by prior phases without changing the current plan or calling Marvin:

```console
marvin-pilot visualize phase-2.json --context-plan phase-1.json
```

`--context-plan` is repeatable. For a plan that has already applied, supply its exact durable
receipt to reconstruct deleted targets and label the result as historical rather than a preview:

```console
marvin-pilot visualize plan.json --receipt applied-20260830T120000Z--receipt-id.json
```

Pilot verifies the receipt hash, requires a fully applied apply receipt, and requires an exact
plan ID and canonical-digest match. Receipt `beforeDocument` records override a newer post-apply
backup for the affected items, while exact IDs remain available only in details/tooltips.

This remains credential-free and makes no Marvin API calls. Pilot indexes active categories,
projects, and tasks locally, combines that snapshot with project creates, renames, moves, and
Trash operations from the plan, and sends only the hierarchy nodes needed by the rendered preview
to the loopback browser page. It does not modify the plan, so its canonical digest is unchanged.
Explicit `display.beforePath`/`display.afterPath` values—including an explicit `null`—always win
over backup inference. Use a current backup: an item absent from the snapshot remains visibly
unresolved rather than being guessed.

The default **Preview** renders Marvin-like **Now** and **After (preview)** hierarchies. With a
verified `--receipt`, those labels become **Before** and **Applied result**, and the status badge
identifies the receipt-backed view. Inbox, categories, projects, tasks, recurrence definitions,
occurrences, and subtasks have distinct Marvin-like visual treatment; projects visibly support
create, rename, move, schedule, complete, and Trash transitions. A generated occurrence keeps its
task circle and shows a compact recurrence-loop icon on the right, while a recurrence definition
omits the task circle and uses the loop icon as its type marker. Tooltips and item details spell out
whether a change affects one generated occurrence or the series template. Completed After cards
show the exact marked-done timestamp using the reviewer's browser locale, time zone, and 12/24-hour
convention. Moved items render under their truthful parent on each side and cross-highlight their
counterpart, while shared hierarchy disclosure stays synchronized. When an explicit chain performs
several operations on one item, Preview shows that item's initial and final boundary states once;
compact `Step 1/3` badges identify the chain, while **Changes** retains every intermediate
operation. Click any row to pin its two states in a sticky comparison tray; moved-item Previous/Next
and Jump controls avoid hunting for a far-away destination. Deep hierarchies scroll horizontally
within each pane, and single-state views provide the full content width.

Use **Day sections: Show/Hide** to layer explicit Today-list groupings over the hierarchy. Day
sections such as Waiting or Main are visually distinct from categories and projects. Switch to
**Changes** for an aligned operation diff grouped by typed After location, Now location, plan order,
or supplied Today section. Uneven before/after cards keep their natural heights, so a short loose
task is not stretched to match a long checklist. Consolidated subtasks use a compact **From source
task** chip; its tooltip retains the full source title and ID without squeezing the subtask title.
Search and the Moved filter keep large cleanups navigable. Action totals filter either mode, and
item details expose the reason, identifiers, exact field diff, hierarchy path, supplied day-section
context, and structured subtask changes.

**Selection: Titles only** is the default: dragging across cards copies complete task/subtask titles
one per line and excludes badges, categories, durations, notes, and controls. Choose **Full cards**
to restore native rich selection. **Titles: Compact** clamps very long titles to four lines in
side-by-side review with an accessible **Show full** control; single-state and pinned comparisons
always retain the full title. Both preferences are stored locally, but plan data is not.

It does not load a Marvin credential, call the Marvin API, persist task data in browser storage, or provide mutation controls. The selected plan is passed through the same strict validator used by `apply`.

Press `Ctrl+C` in the launching terminal to stop it.

Typed `display.beforePath` and `display.afterPath` metadata supplies offline ancestry without
affecting apply. Empty paths mean a known Marvin root. For omitted paths, Pilot first resolves IDs
from other project operations in the plan and then from optional `--backup` context; anything it
still cannot prove renders under **Location not supplied** instead of being guessed. Completion is
the safe exception: when a known `beforePath` is present and `afterPath` is omitted, the completed
After state inherits that unchanged ancestry. Explicit `afterPath: null` remains unknown. Optional
path-node and target order values reproduce sibling ordering, while legacy
`beforeSection`/`afterSection` values remain a visibly inferred fallback.

An update or Trash operation on an already-completed task may provide its original RFC 3339 timestamp as `display.existingCompletedAt`. Preview then renders the task as completed, with the localized completion timestamp, in every state where it exists. Live preflight requires this exact metadata before updating a completed task and verifies it against Marvin's `doneAt`; the review-only field never becomes a setter. Reparenting therefore changes only `parentId` and preserves both `done` and `doneAt` through apply and revert.

The browser suite includes synthetic hierarchy, lifecycle, movement, and subtask cases. Maintainers
can point `MARVIN_PILOT_PRIVATE_PLAN_DIR` at an ignored local regression corpus containing
`01.json` through `04.json`; private fixtures must never be committed.

The visible header keeps the full Marvin Pilot illustration, while the browser tab uses a dedicated
64×64 favicon. Do not point the favicon link back at the large header artwork: Firefox persists tab
icons in session-restore data, so oversized favicons can multiply storage and CPU costs across
retained tabs.

## Plans and recovery

Plans are versioned, closed-schema JSON documents containing stable `operationId` values and typed `create`, `update`, `complete`, or `trash` operations. Targets can be tasks, projects, categories, entire recurring-task series, or explicitly identified generated occurrences. A category or recurrence series cannot itself be completed; complete a project or one generated occurrence instead.

Generate the authoritative schema and example directly from the installed CLI:

```console
marvin-pilot schema --output change-plan.schema.json
marvin-pilot schema --kind draft --output change-draft.schema.json
marvin-pilot schema --kind plan-set --output plan-set.schema.json
marvin-pilot example --output plan.json
marvin-pilot help plan-format
```

V1 supports common task and project fields including titles, parents/categories, dates and scheduling, labels, estimates, notes, ranks, sections, priorities, backburner state, review dates, and snooze values. Tasks additionally support star priority, `masterRank`, dependencies, ordered embedded `subtasks`, and the Orbit toggle. JSON `null` clears a supported value. Category edits intentionally support only title and parent.

Existing targets use exact IDs plus required title safety hints. Target, recurrence-series, and
`sourceTask` titles remain hard live preconditions. Optional `parent.title` and label titles are
review hints: stale values produce prominent warnings containing the exact live replacement but do
not block an otherwise identity- and concurrency-safe plan. Pilot never applies fuzzy emoji or
whitespace normalization during live identity checks. Backup candidate search may normalize Unicode,
leading emoji/decorations, case, and whitespace only to list candidates; ambiguity never selects an ID.

An update can express stable relative ordering with `siblingOrder: {"beforeId":"…"}`,
`{"afterId":"…"}`, or `{"position":"first|last"}`. Live preflight verifies that the anchor is a
compatible sibling on the task's final scheduled day or under its final parent, then writes Marvin's
native rank. Revert restores the original rank. Raw rank edits and `siblingOrder` cannot be mixed.

Reminder records are separate coupled server state. Pilot does not currently mutate or promise to
preserve them, so live preflight blocks affected documents with an explicit instruction to remove
and recreate the reminder in Marvin or make that change by hand. Calendar synchronization,
active time tracking, reward side effects, and pinned-task copying remain similar hard blockers.

One plan may intentionally apply several operations to the same existing target—for example
rename, move, then complete—when every later step directly lists the immediately preceding same-target
`operationId` in `dependsOnOperations`. Each later `target.title` must equal the title produced by
the preceding step, and only the first operation in the chain may carry `expectedUpdatedAt`.
Trash is terminal. Live validation projects each successful intermediate state, so later field and
hierarchy checks see the planned rename/move rather than stale live state. Apply and reverse-order
revert replace projected revisions with the actual revision read after every write, retaining
strict concurrency protection throughout the chain.
Changes to a newly created item must stay coalesced in its single `create` operation; an immediate
create-then-complete/Trash sequence is rejected as an avoidable or contradictory lifecycle.

Each subtask has a stable `id`, exact `title`, and `done` state; array order becomes native Marvin rank. Retained subtask records are merged by ID so unknown native metadata survives. Omission removes a prior subtask and `null` clears the checklist. A new subtask may include review-only `sourceTask: {id, title, acceptLoss?}` when consolidating a loose task, but only with a later dependent `trash` operation. Marvin-assigned ordering/history bookkeeping such as `rank`, `masterRank`, `firstScheduled`, and `workedOnAt` is expected to disappear during conversion and needs no acknowledgment. Meaningful fields that a subtask cannot represent—such as an estimate or note—must be named exactly in `acceptLoss`; unused or missing acknowledgments fail live validation. Existing source subtasks, dependencies, recurrence, completion, and tracked-time history remain hard conversion blockers. Accepted losses appear in descriptions, live/apply warnings, and visualizer badges; receipts retain the source plan and complete trashed task for recovery.

Project creates write native `Categories` documents with `type: "project"`; updates can rename, move, or edit allowlisted fields; completion records an explicit historical RFC 3339 timestamp; and Trash uses the same receipt-backed deletion as tasks. Planned project ancestry is checked before writes, including parents created earlier in the same plan and cycle prevention.

Category creates use the same database with `type: "category"`. Categories can be created,
renamed, moved, reordered, and deleted, but not completed. Before deleting any project or category,
live preflight calls Marvin's direct-children endpoint and refuses while a child remains. Child
moves/deletions earlier in the same dependency chain are projected, so a plan can explicitly empty
then delete a container. Container Trash remains blocked from unattended apply regardless of the
nominal one-operation impact.

Task and project completion write the exact `doneAt` instant and assign Marvin's `day` to the local
calendar date encoded by that timestamp's explicit RFC 3339 offset. Projects additionally write
the same date to `doneDate`. This is the native task behavior confirmed for overdue, same-day,
future-scheduled, and unscheduled items; a missing/unassigned day is assigned, while any other
scheduled day is replaced unless it already matches. Marvin's `fieldUpdates.done`,
`fieldUpdates.doneAt`, `fieldUpdates.day`, project `fieldUpdates.doneDate`, and `updatedAt` retain
the real apply time, matching the native client while keeping concurrency truthful.

Prepared and rebased task/project-completion plans include review-only
`completionDay: {before, after, behavior}` metadata. `before` is the current usable day or `null`,
`after` is the timestamp's local date, and `behavior` is `assigned`, `preserved`, or `replaced`.
Live preflight rejects a stale `before` lock. Older plans that omit this optional metadata remain
compatible: live preflight still derives and verifies the same setter, while `prepare`/`rebase`
adds the explicit lock for human review.

Projects completed by Pilot versions that predate native `doneAt` support can be audited from their
original receipts. `marvin-pilot history audit RECEIPT --live --repair-plan REPAIR.json` emits a
separate `complete` operation with `repairHistory: true` only when the project is still completed,
its `doneAt` is absent, and its `doneDate` agrees with the original reviewed `completedAt`. Live
preflight locks the title, `updatedAt`, current history day, and `doneDate`, and refuses to overwrite
any existing completion timestamp. The repair remains review-only until explicitly applied and is
fully revertible from its own receipt.

Completed tasks remain ordinary task documents addressable by exact ID. Pilot can rename or reparent them without reopening them, but the plan must include the verified `display.existingCompletedAt` timestamp so the historical state is visible during review. This supports backup-assisted historical reorganization while retaining a fresh live-state and concurrency check before every write.

### Dependency-ordered plan sets

For a cleanup that must remain in several reviewable files, a `planSetVersion: 1` manifest lists
safe relative child paths and optional `dependsOn` phase paths. Every child has the same exact
`expectedAccount`, and operation IDs are unique across the set. The usual `validate`, `describe`,
`visualize`, `apply`, `history status`, and `revert` commands accept the manifest directly.

```json
{
  "planSetVersion": 1,
  "planSetId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "summary": "Create the structure, then reorganize it.",
  "expectedAccount": {"userId": "123456", "email": "account@example.com"},
  "plans": [
    {"path": "01-structure.json"},
    {"path": "02-reorganize.json", "dependsOn": ["01-structure.json"]}
  ]
}
```

Live validation projects successful earlier phases over a shared read cache, apply asks once and
runs phases forward, and revert uses the recorded child receipts in reverse dependency order.
Plan-set parent receipts are integrity checked and link every child receipt. `revert --only` can
select operation IDs across phases.

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

One upstream distinction matters for audits: Pilot verifies the full task/project document after
apply, including `done`, `doneAt`, and the completion-history `day` (plus project `doneDate`); it
does not claim that the separate, limited-token `/doneItems` endpoint was checked. Receipts expose
both facts explicitly. Use `history audit --live` for authoritative full-document comparison. For
older receipts created before Pilot wrote task `day` or project `doneAt`, the same command can
diagnose the defect and `--repair-plan PATH` can emit a separately reviewable correction.

### Pilot-managed Trash and recovery

Marvin's native Trash is client-side: the app saves a copy in browser-local storage and then deletes the synced document. The public API cannot add an item to that local Trash. Pilot's `trash` action therefore uses `/doc/delete`, but only after a pending receipt containing the complete original document has been durably written. Successful post-write verification requires the exact document ID to be absent. This fixes the former `deletedAt`-only behavior, which left pseudo-trashed tasks live and visible in Today.

Pilot recovery is receipt-backed rather than Marvin-native. `marvin-pilot revert RECEIPT.json` recreates the same ID from the stored document after removing stale CouchDB `_rev`/`_deleted` fields. Reverting a Pilot `create` likewise deletes the created document instead of leaving a hidden live record. Neither deletion appears in Marvin's native Trash UI.

Unattended mode never trashes a project or category. A container Trash receipt contains that document
itself, not an independently enumerated snapshot of every descendant, so this container-level
operation continues to require interactive review even when the configured unattended limit would
otherwise permit one operation.

Every live apply writes audit state before the first mutation and records field-scoped before/after data and per-operation outcomes. Trash receipts include the full original task, project, or recurrence-template document—including notes and other personal content—because that snapshot is the recovery source. Keep the history directory private and do not commit or share receipts casually.

Revert is conflict-aware: updates and completions restore only fields changed by the original apply and preserve unrelated later edits. A trashed document is restored only while its ID remains absent. A created document is deleted on revert only when its complete post-create snapshot is unchanged.

Receipts include a SHA-256 integrity hash to detect accidental modification. The hash is **not** a cryptographic signature and does not make history tamper-proof.

## Safety model

The intended boundary is explicit:

**AI:** read tasks, draft/validate/explain plans, and apply only a user-requested change through an
already enabled unattended policy.

**Human:** review normal plans, run `apply`/`revert`, and choose whether to enable or disable an
account-pinned unattended cap.

Normal `apply` and every `revert` require an interactive controlling terminal and an explicit `y`
or `n` decision; empty input is never approval or decline. A human may opt into `apply --yes` after
reviewing a plan; it skips the final decision but not the terminal requirement or any safety check.
`apply --unattended` works only within the locally enabled account and impact policy. Revert has no
equivalent bypass.

The full-access credential is retrieved only when a live command needs it and is never written into plans or receipts.

This is a workflow and credential boundary, not an operating-system sandbox. Software running as
the same user may still be able to invoke the CLI, edit its non-secret policy file, or interact with
the system credential service. The unattended cap protects against accidental oversized plans; it
is not a security boundary against malicious local software. Use `prompt` or carefully permissioned
`file` mode if that threat matters to you.

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

Live contract testing has covered the v1 task and project field set, project CRUD, historical
completion, ordered subtask create/read/update/delete/reorder/complete/reopen/consolidation,
recurrence-series create/read/update/Trash/restore/revert, explicit generated-occurrence
update/complete/Trash/revert, create/update/schedule/unschedule/Trash/restore/revert workflows, and
a 200-operation scale run against a dedicated development account. Native task completion-day
behavior was separately checked across overdue, same-day, future, unscheduled, and cross-day
recurrence cases. Native completed-project documents were also cross-checked for `doneAt`, `day`,
`doneDate`, and real write-time field-update semantics. Recurrence and completion scope have unit,
in-memory integration, and browser-visualizer coverage; their disposable live-account contracts
are described in `contract-tests/README.md`.

See [`contract-tests/README.md`](contract-tests/README.md) before running live contract cases.

Developer investigation notes, live-account reports, and private visual-regression corpora belong
under the ignored `dev/` directory. Durable setup, safety, and supported behavior must be documented
in this README or the contract-test guide so a checkout is safe to share by default.

## Project status

Marvin Pilot is currently **pre-alpha**. The planned PyPI distribution name is `amazing-marvin-pilot`; public PyPI publishing has not begun.

Marvin Pilot is an independent community project and is not affiliated with or endorsed by Amazing Marvin.
