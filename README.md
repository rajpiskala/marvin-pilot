# Marvin Pilot

**Your AI plans. You approve. Marvin Pilot applies.**

Marvin Pilot is the safe action companion to
[Amazing Marvin MCP](https://github.com/bgheneti/Amazing-Marvin-MCP). It accepts a
human-readable JSON change plan, validates it against live Amazing Marvin state, asks a human to
approve the exact changes, applies them with a separately held full-access credential, and writes a
durable receipt for selective or complete compensating revert.

> [!WARNING]
> Marvin Pilot is pre-alpha. Live mutation support must not be used with a production Amazing
> Marvin account until the documented development-account contract tests have passed.

## Intended workflow

1. An AI assistant reads your tasks through the limited-access Amazing Marvin MCP.
2. The assistant writes a versioned Marvin change plan.
3. You run `marvin-pilot describe plan.json` and review the proposal.
4. You run `marvin-pilot apply plan.json` in an interactive terminal.
5. If necessary, you run `marvin-pilot revert RECEIPT.json`, optionally with multiple repeated
   `--only OPERATION_ID` selections.

The AI should not receive the Amazing Marvin full-access token and should not invoke `apply` or
`revert`. Permanent deletion is intentionally unsupported; deletion proposals use Marvin's
reversible Trash behavior.

## Development

Marvin Pilot requires Python 3.11 or newer.

```console
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

The complete design, schema decisions, security model, and rollout gates are documented in
[`Implementation-Plan.md`](Implementation-Plan.md).

## Project status

Implementation is in progress. Offline validation and description will land before any command is
allowed to mutate live Marvin data. Unit tests and mocked HTTP integration tests run locally;
contract and browser tests will later run only against a dedicated development Marvin environment.

Marvin Pilot is an independent community project and is not affiliated with or endorsed by Amazing
Marvin.
