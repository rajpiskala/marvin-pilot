# Changelog

All notable changes to Marvin Pilot are documented here.

## Unreleased

- Reuse running visualizer sessions for the same inputs and live-reload watched plans, context
  plans, backups, and receipts while preserving review state.
- Add optional `visualize --allow-apply` with account pinning, exact-file freshness checks,
  live preflight, explicit browser confirmation, CSRF protection, and the usual recovery receipt.
- Make Preview/Changes transitions atomic and preserve expansion, selection, filters, and scroll.

## 1.0.1 — 2026-09-18

- Fix the PyPI description's mascot image and documentation links by using public absolute URLs.
- Fix GitHub release asset upload when the publishing job runs without a checkout.

## 1.0.0 — 2026-09-18

First public stable release.

### Highlights

- Strict, account-pinned change plans with offline and live validation.
- Compact draft preparation and verified live context for Today and project workflows.
- Task, project, category, subtask, recurrence, and historical-completion changes.
- Hierarchical before/after visual review with operation filters and detail views.
- Interactive approval and configurable bounded unattended apply.
- Integrity-checked receipts with full and selective conflict-aware revert.
- Backup-powered historical project analysis without uploading backup data.

### Known limitations

- Marvin Pilot cannot preserve every coupled or undocumented Amazing Marvin feature.
- Pilot-managed deletion uses Marvin's permanent document API and depends on the private receipt
  snapshot for recovery; deleted items do not appear in Marvin's native Trash UI.
- Installation and live use require Python 3.11 or newer and an Amazing Marvin account with API
  access enabled.

Back up Marvin before first use. See the README safety model and contract-test runbook before
relying on Marvin Pilot for important workflows.
