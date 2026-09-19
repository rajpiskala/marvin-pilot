<p align="center">
  <img src="https://raw.githubusercontent.com/rajpiskala/marvin-pilot/main/src/marvin_pilot/visualizer_assets/marvin-pilot.png" alt="Marvin Pilot mascot" width="360">
</p>

<h1 align="center">Marvin Pilot</h1>

<p align="center"><strong>Your AI plans. You approve. Marvin Pilot applies.</strong></p>

<p align="center">
  <a href="https://github.com/rajpiskala/marvin-pilot/actions/workflows/ci.yml"><img src="https://github.com/rajpiskala/marvin-pilot/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="https://pypi.org/project/marvin-pilot/"><img src="https://img.shields.io/pypi/v/marvin-pilot.svg?cacheSeconds=300" alt="PyPI version"></a>
  <img src="https://img.shields.io/badge/status-stable-brightgreen.svg" alt="Project status: stable">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg?logo=python&logoColor=white" alt="Python 3.11 or newer">
  <a href="https://github.com/rajpiskala/marvin-pilot/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
  <a href="https://github.com/sponsors/rajpiskala"><img src="https://img.shields.io/badge/sponsor-rajpiskala-EA4AAA.svg?logo=githubsponsors&logoColor=white" alt="Sponsor Marvin Pilot"></a>
</p>

<p align="center">
  <a href="#-why-marvin-pilot">Why</a> ·
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-your-first-plan">First plan</a> ·
  <a href="#-safety-at-a-glance">Safety</a> ·
  <a href="https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md">Full reference</a> ·
  <a href="#-sponsors">Sponsors</a>
</p>

Marvin Pilot is a local safety layer between an AI assistant and [Amazing Marvin](https://amazingmarvin.com/). The AI drafts an exact change plan; Pilot validates it against live state, shows you the result, applies only what you approve, and writes a receipt that can be reverted.

Your full-access Marvin credential stays on the Pilot side of the workflow instead of being placed in an AI prompt, plan, MCP configuration, command-line argument, or environment variable.

> [!IMPORTANT]
> Back up Marvin before your first use and review the documented limitations. Some coupled or undocumented Marvin features cannot be preserved safely.

## ✨ Why Marvin Pilot?

- **Review the whole change.** Inspect a terminal summary or local before/after visualizer before anything is written.
- **Catch stale plans.** Account, title, hierarchy, timestamp, recurrence, and other locks are checked against live Marvin data.
- **Keep an escape route.** Every apply creates an integrity-checked receipt for conflict-aware full or selective revert.
- **Choose the approval boundary.** Confirm each normal apply, or explicitly preauthorize a small account-pinned impact limit for routine work.
- **Keep private data local.** Plans, backups, receipts, credentials, and the visualizer stay on your machine.

| | AI / read-only MCP | Marvin Pilot |
| --- | --- | --- |
| Credential | Limited `API_TOKEN` | `FULL_ACCESS_TOKEN` |
| Role | Understand work and draft a plan | Validate, review, apply, verify, recover |
| Mutations | None in the intended workflow | Interactive or locally preauthorized |
| Recovery | — | Receipt-backed revert |

## 🧭 How it works

```text
Marvin context  →  AI draft  →  prepare + validate  →  review  →  apply
                                                                  │
                                                                  └─ receipt → revert
```

1. An AI reads current work through a limited-access MCP, verified Pilot context, or a local Marvin backup.
2. The AI writes a strict plan or compact draft. `prepare` fills verified locks and produces the exact review artifact.
3. You run offline and read-only live validation, then inspect the terminal or browser preview.
4. Pilot rechecks current state immediately before each write, verifies the result, and journals a receipt.

## 🚀 Quick start

Marvin Pilot requires Python 3.11 or newer. Install the stable release from PyPI with [pipx](https://pipx.pypa.io/):

```console
pipx install "marvin-pilot==1.0.1"
marvin-pilot --version
```

To install the current source checkout instead:

```console
git clone https://github.com/rajpiskala/marvin-pilot.git
cd marvin-pilot
pipx install .
```

In Amazing Marvin, open **Settings → API** and create both credentials:

- Give the read/discovery MCP only the limited `API_TOKEN`.
- Store `FULL_ACCESS_TOKEN` for Marvin Pilot, preferably in your operating system credential store.

```console
marvin-pilot config set-credential-mode keyring
marvin-pilot config set-full-access-token
marvin-pilot doctor
```

`doctor` makes one read-only identity request and confirms which Marvin account the credential belongs to. See the [setup reference](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md#quick-start) for MCP launcher configuration, platform-specific commands, and stricter credential modes.

## 🛫 Your first plan

Start small. Ask your AI:

> Read my Marvin tasks through the Amazing Marvin MCP. Draft a Marvin Pilot change plan with at most 10 low-risk operations and save it under `plans/`. Do not use MCP mutation tools and do not run `marvin-pilot apply` or `marvin-pilot revert`; I will review and run those myself.

Then review the same artifact at every stage:

```console
marvin-pilot validate plans/first-plan.json
marvin-pilot validate plans/first-plan.json --live
marvin-pilot describe plans/first-plan.json
marvin-pilot visualize plans/first-plan.json
marvin-pilot apply plans/first-plan.json
```

If you need to undo it:

```console
marvin-pilot revert path/to/applied-receipt.json
```

The full guide covers [compact draft preparation](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md#your-first-plan), [bounded unattended apply](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md#bounded-unattended-apply), [backup-powered historical context](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md#historical-project-context-from-a-backup), and [dependent plan sets](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md#dependency-ordered-plan-sets).

## 🧰 Core commands

| Command | Purpose |
| --- | --- |
| `marvin-pilot doctor` | Verify the configured account and full-token health |
| `marvin-pilot context …` | Read bounded live or backup-powered Marvin context |
| `marvin-pilot prepare INPUT …` | Compile a compact draft or safely refresh plan locks |
| `marvin-pilot validate PLAN [--live]` | Validate offline or collect read-only live diagnostics |
| `marvin-pilot describe PLAN` | Render a text, Markdown, or JSON review |
| `marvin-pilot visualize PLAN` | Open the local hierarchical before/after view |
| `marvin-pilot apply PLAN` | Preflight, approve, apply, verify, and write a receipt |
| `marvin-pilot revert RECEIPT` | Revert all or selected operations safely |
| `marvin-pilot history …` | Inspect, verify, and audit local receipts |

Run `marvin-pilot --help`, `marvin-pilot COMMAND --help`, or read the [complete command reference](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md#core-commands).

## 🛡️ Safety at a glance

- Normal apply and every revert require an interactive terminal and explicit approval. `apply --yes` skips only the final prompt after review; it does not skip validation or verification.
- Optional unattended apply must be enabled by a human in advance and is pinned to one account and a maximum impact. It is an accident guard, not a security boundary against malicious local software.
- The full-access credential is loaded only for live commands and is never written to a plan or receipt.
- Pilot-managed deletion uses Marvin's document API, not Marvin's native Trash UI. Recovery depends on the private receipt snapshot, so keep receipts secure and backed up.
- Plans and receipts may contain personal task content. `plans/`, local history, backups, and development artifacts should never be committed casually.

Read the [full safety model](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md#safety-model) and [security policy](https://github.com/rajpiskala/marvin-pilot/blob/main/SECURITY.md) before using Marvin Pilot on important workflows.

## 📚 Documentation

- [Comprehensive user and behavior reference](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md)
- [Live contract-test runbook](https://github.com/rajpiskala/marvin-pilot/blob/main/contract-tests/README.md)
- [Release notes](https://github.com/rajpiskala/marvin-pilot/blob/main/CHANGELOG.md)
- [Security policy](https://github.com/rajpiskala/marvin-pilot/blob/main/SECURITY.md)

GitHub automatically provides an outline for this README. The compact links at the top cover the common path; detailed navigation lives in the reference guide.

## 💖 Sponsors

If Marvin Pilot saves you time, [sponsor its continued development](https://github.com/sponsors/rajpiskala). Sponsorship helps fund maintenance, testing across Marvin workflows, and safer automation features.

## 🧑‍💻 Development

```console
python -m venv .venv
python -m pip install -e ".[dev]"
python -m pytest --cov=marvin_pilot
python -m ruff format --check src tests
python -m ruff check src tests
```

See the [development reference](https://github.com/rajpiskala/marvin-pilot/blob/main/docs/reference.md#development) before running browser or live-account contract tests.

## 📌 Project status

Marvin Pilot is an independent community project and is not affiliated with or endorsed by Amazing Marvin. The repository, CLI command, import package, and PyPI distribution all use the **Marvin Pilot** name.

Released under the [MIT License](https://github.com/rajpiskala/marvin-pilot/blob/main/LICENSE).
