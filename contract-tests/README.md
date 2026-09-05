# Marvin Pilot reusable contract-test kit

This directory turns the dedicated-account E2E run into reproducible evidence. It contains a
sample-only generated suite, both authoring schemas, a live account-config template, stable case
IDs, expected outcomes, per-file SHA-256 hashes, cleanup instructions, and a bug-report template.

> [!CAUTION]
> The checked-in `samples/` plans contain deliberately fake reference IDs and titles beginning
> with `DO NOT APPLY`. They are for offline inspection and validation only. Generate a fresh suite
> under the ignored `plans/` directory before running any live command. Use a disposable Marvin
> account or development backup.

The generator never reads a credential or calls Marvin. `generate` and `verify` are offline. The
normal human boundary still applies to every generated `apply` and `revert`: review the plan, run
the command yourself in a controlling terminal, and retain its receipt.

## What is bundled

- `samples/manifest.json` is the machine-readable case catalog. It records case IDs, matrix IDs,
  expected offline/live results, instructions, and hashes.
- `samples/*.json` contains 25 synthetic, offline-valid sample plans covering the live progression.
- `samples/change-plan-v1.schema.json` is the exact plan schema snapshot used for the samples.
- `samples/account-config-v1.schema.json` describes the optional live-reference config.
- `account.example.json` is the file to copy into ignored local storage and fill with real IDs.
- `sample-account.json` is the explicitly non-live config used to reproduce the committed samples.
- `evidence-report-template.md` gives feature reports a consistent, auditable shape.

The installed CLI can always emit the current account schema:

```console
marvin-pilot contract-tests account-schema
```

## Verify the bundled sample

This checks every manifest hash and parses every plan according to its expected validity:

```console
marvin-pilot contract-tests verify contract-tests/samples
```

The committed snapshot uses a 10-operation scale case to stay small. The generator defaults to
200 for a release-scale run.

## Prepare a live suite

1. Copy `account.example.json` to an ignored path such as `plans/contract-account.json`.
2. Using the limited Amazing Marvin MCP or UI, replace only the IDs/titles for references that
   exist in the disposable account. The file never contains an API key.
3. Remove optional keys for fixtures the account does not have. The generator will report the
   resulting coverage gap rather than invent a live ID.
4. Set `sampleOnly` to `false` only after every `replace-with-*` placeholder has been replaced or
   removed. The generator rejects live configs that retain a placeholder.
5. Choose a base date far enough in the future that schedule/snooze behavior is easy to inspect.
6. Generate into a new ignored directory. Omitting `--run-id` gives every run fresh plan/task IDs.

```console
marvin-pilot contract-tests generate plans/contract-run-001 \
  --base-date 2026-08-10 \
  --account-config plans/contract-account.json \
  --scale-count 200 \
  --include-limit-cases

marvin-pilot contract-tests verify plans/contract-run-001
```

`--include-limit-cases` adds offline-only 500-operation-valid and 501-operation-invalid plans. Do
not apply either boundary plan. Use `--run-id UUID` and `--created-at RFC3339` only when exact
deterministic reproduction is more important than generating a fresh live identity.

The generator refuses to overwrite a non-empty directory. Generated manifests and plans can
contain account IDs and therefore belong under ignored `plans/`, not Git.

## Ordered live workflow

Read the generated `manifest.json` before starting. Its case notes are authoritative for that
suite. The high-level progression is:

| Files | Expected behavior | Primary coverage |
|---|---|---|
| `01-create-fixtures` | Apply succeeds; retain receipt until final cleanup. | Create shape, dependencies, audit |
| `02a-*` through `02h-*` | Each applicable case exits 5 during live preflight with zero writes. | Stale state, references, task boundary, collision |
| `03` through `06` | Apply in order; inspect after each; revert in reverse order. | Every configured field, ordered subtask add/remove/rename/reorder/complete/reopen, null clears, enum extremes |
| `07` and `08` | Reverting 07 while 08 is applied must conflict; then revert 08 and 07. | Touched-field conflict |
| `09` and `10` | Reverting 09 must preserve the note from 10; then revert 10. | Unrelated-field preservation |
| `11` through `13` | Receipt-backed delete succeeds; update/re-delete fail; reverting 11 recreates the same ID. | API absence and Pilot recovery |
| `14` | Apply, then use repeated `--only` for two operation IDs. | Ordered create and multi-ID selective revert |
| `15` and `16` | Apply 15 through stdin with a controlling TTY; apply/revert 16, then 15. | Stdin durability, pacing, progress, ETA |
| `17` | Apply, verify scheduling/`firstScheduled` and create-time subtask order/completion, then revert to Trash. | Rich scheduled create and create inverse |
| `18` | Apply and revert the configured scale count, resuming partial receipts if necessary. | Backoff, reconciliation, audit scale |
| `19a` and `19b` | Offline validation only: 500 passes and 501 fails. | Maximum-operation boundary |

For any successful plan:

```console
marvin-pilot describe plans/contract-run-001/03-set-all-fields.json
marvin-pilot apply plans/contract-run-001/03-set-all-fields.json
marvin-pilot history verify path/to/applied-receipt.json
marvin-pilot revert path/to/applied-receipt.json
```

For the selective-revert case, one command accepts multiple operation IDs:

```console
marvin-pilot revert path/to/applied-14-receipt.json \
  --only ordered-create-1 \
  --only ordered-create-3
```

Never automate answers to the approval prompt or add `apply --yes` to a public test runner. The
human terminal boundary is part of the contract being tested; `--yes` is only a convenience after
a person has reviewed that specific plan.

### Recurrence contract extension

Use unique, sanitized Inbox fixtures in the dedicated development account. Exercise an entire
series with `target.type: "recurringTask"`, then exercise individual native generated occurrences
with `target.type: "task"` plus the exact `target.recurrence` series ID, series title, and scheduled
date. The minimum live matrix is:

1. Create and read a series with an explicit cadence and ordered template subtasks.
2. Rename it, change cadence and indicators, and reorder/add template subtasks.
3. Update one generated occurrence, complete a second, and Trash a third.
4. Verify the receipt, revert the occurrence operations in reverse order, and confirm exact state.
5. Delete and restore the complete series, then finish by deleting every fixture through receipt-backed Trash.

Use a full-document read and the synced browser database as independent oracles. Confirm the
browser distinguishes the template (`db: "RecurringTasks"`) from generated tasks (`db: "Tasks"`,
`recurring: true`, and the exact `recurringTaskId`). Do not reuse or mutate a person's real
recurrence series as a fixture. Marvin owns occurrence generation; a same-day series created by the
remote document API may sync without immediately backfilling an occurrence during a browser reload.

### Completed-task reparent contract extension

Use two disposable projects and one disposable ordinary task in the dedicated development account:

1. Complete the task with an explicit historical timestamp and retain its full document.
2. Update only its parent using `display.existingCompletedAt` with that exact timestamp.
3. Verify through a full-document read and the browser that `parentId` changed while `done: true`
   and `doneAt` remained byte-for-byte unchanged.
4. Revert the move and verify the original parent and completion metadata are restored/preserved.
5. Delete the fixtures through Pilot-managed Trash and retain the private recovery receipts locally.

Also preview the move before applying it. Both Now and After must show the completed task marker and
localized original completion timestamp under their respective project hierarchies.

### Completion-history day contract extension

Use disposable ordinary tasks that begin overdue, scheduled today, future-scheduled, and
unscheduled. Complete each with an explicit timestamp whose offset resolves to the chosen
completion date. For every case verify that:

1. `doneAt` is the exact requested instant and `day` is that instant's local `YYYY-MM-DD`.
2. `fieldUpdates.done`, `fieldUpdates.doneAt`, `fieldUpdates.day`, and `updatedAt` describe the
   actual mutation time rather than pretending that the API write happened historically.
3. The receipt stores the full before/after documents, marks the document day verified, and leaves
   separate server-history visibility explicitly `not-checked`.
4. The visualizer shows both the localized marked-done timestamp and the history day.
5. Revert restores the exact prior `day`, including the original date of a generated occurrence.

Include a generated recurrence occurrence whose identity date differs from its completion date.
Its apply receipt must retain the original recurrence identity while its verified after-state uses
the completion day; reverse-order revert must accept that transition and restore the identity day.
Do not infer native behavior from a single same-day task, because that misses the replacement and
recurrence cases.

## Independent live oracles

Use at least two independent views for mutations:

1. Marvin Pilot post-write verification plus `history verify`.
2. Full-access single-document read for exact stored fields.
3. Limited Amazing Marvin MCP where it exposes the value.
4. Browser/UI or browser-side PouchDB for schedule, visibility, Trash, and `_deleted` behavior.

For Trash, verify the full-access single-document read returns Marvin's deleted/not-found result,
Today no longer returns the item, and the apply receipt contains the complete pre-delete document.
Then revert and verify the same ID and business fields were recreated without the stale `_rev`.
The item will not appear in Marvin's browser-local native Trash. For `estimatedTimeDuration`,
verify `timeEstimate` changes and tracked `duration` does not.

## Cleanup and interrupted runs

Revert dependent receipts in reverse order. Revert the original `01-create-fixtures` receipt last;
this deletes any remaining created fixtures after verifying that their complete post-create
documents are unchanged.

If an interrupted journal contains no `sending`, `verifying`, `unknown`, or `reverting` operation,
it can be terminalized without a Marvin call:

```console
marvin-pilot history finalize-interrupted path/to/pending-receipt.json
```

Resume from the resulting partial receipt, selecting all remaining operation IDs as needed. Verify
the aggregate confirmed IDs match the source plan before declaring cleanup complete.

## Reproducing a reported failure

Generate a fresh run with the same Marvin Pilot version and the same account capability flags.
Start at case 01 and stop at the smallest failing case; do not reuse someone else's task IDs. Attach
a filled copy of `evidence-report-template.md`, but sanitize task content and never attach tokens,
credential files, or unsanitized receipts.

Live-account evidence and maintainer run notes may contain account-derived details and belong under
the repository's ignored `dev/` directory. Keep only sanitized, reusable contract fixtures here.

## Maintaining the checked-in snapshot

The sample snapshot is deterministic. Generate it into a new temporary directory with:

```console
marvin-pilot contract-tests generate path/to/new-sample \
  --base-date 2026-08-10 \
  --account-config contract-tests/sample-account.json \
  --run-id 22222222-2222-4222-8222-222222222222 \
  --created-at 2026-08-08T20:00:00-07:00 \
  --scale-count 10
```

Review the manifest and plan diffs before intentionally replacing `samples/`. The automated suite
also regenerates this exact data in memory and fails if the committed snapshot drifts.
