"""Marvin Pilot command-line interface."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from marvin_pilot import __version__
from marvin_pilot.approval import confirm_apply, confirm_revert
from marvin_pilot.config import (
    AppConfig,
    default_config_path,
    default_history_dir,
    effective_history_dir,
    load_config,
    save_config,
)
from marvin_pilot.credentials import (
    delete_keyring_token,
    load_full_access_token,
    store_keyring_token,
)
from marvin_pilot.describe import (
    render_live_preflight,
    render_plan_description,
    render_revert_preflight,
)
from marvin_pilot.errors import MarvinPilotError, PlanSemanticError, PlanSyntaxError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.executor import execute_apply, unix_milliseconds
from marvin_pilot.history import HistoryStore
from marvin_pilot.marvin_client import MarvinClient
from marvin_pilot.plan_io import MAX_PLAN_BYTES, load_plan, parse_plan_bytes, plan_digest
from marvin_pilot.preflight import preflight_plan
from marvin_pilot.reverter import execute_revert
from marvin_pilot.schema import plan_schema_json

SAFETY_CONTRACT = """This CLI separates AI-authored proposals from human-authorized
Marvin mutations.
AI assistants: generate, validate, and describe plans. Do not invoke apply or revert.
Humans: review the exact diff and digest, then run apply or revert in an interactive terminal.
"""

app = typer.Typer(
    name="marvin-pilot",
    help=(
        "Safe, human-reviewed and reversible AI actions for Amazing Marvin.\n\n" + SAFETY_CONTRACT
    ),
    no_args_is_help=True,
    rich_markup_mode=None,
)
help_app = typer.Typer(help="Detailed reference material for plan authors.")
config_app = typer.Typer(
    help="Configure non-secret settings and the native OS keyring.",
    invoke_without_command=True,
)
history_app = typer.Typer(help="Inspect and verify durable apply/revert receipts.")
app.add_typer(help_app, name="help")
app.add_typer(config_app, name="config")
app.add_typer(history_app, name="history")
console = Console(stderr=False)
error_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"marvin-pilot {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Safe, reviewed task changes for Amazing Marvin."""


def _fail(error: MarvinPilotError) -> None:
    error_console.print(f"Error: {error}", markup=False)
    raise typer.Exit(error.exit_code)


def _read_plan_argument_with_bytes(path: str):
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_PLAN_BYTES + 1)
            return parse_plan_bytes(raw), raw
        return load_plan(Path(path))
    except MarvinPilotError as exc:
        _fail(exc)


def _read_plan_argument(path: str):
    return _read_plan_argument_with_bytes(path)[0]


def _write_or_print(content: str, output: Path | None) -> None:
    if output is None:
        typer.echo(content, nl=False)
        return
    try:
        with output.open("x", encoding="utf-8", newline="\n") as file:
            file.write(content)
    except FileExistsError:
        _fail(PlanSyntaxError(f"refusing to overwrite existing file: {output}"))
    except OSError as exc:
        _fail(PlanSyntaxError(f"could not write {output}: {exc}"))
    typer.echo(str(output))


def _load_config_or_fail() -> AppConfig:
    try:
        return load_config()
    except MarvinPilotError as exc:
        _fail(exc)


def _config_summary(config: AppConfig) -> str:
    return (
        f"Config: {default_config_path()}\n"
        f"History: {effective_history_dir(config)}\n"
        f"Credential mode: {config.credential_mode}\n"
        f"Strict concurrency: {'on' if config.strict_concurrency else 'off'}\n"
        f"Minimum request interval: {config.minimum_request_interval_ms} ms"
    )


def _credential_warning(message: str) -> None:
    error_console.print(f"Warning: {message}", markup=False)


def _client_from_config(config: AppConfig, key_file: Path | None) -> MarvinClient:
    try:
        token = load_full_access_token(
            config,
            key_file_override=key_file,
            warn=_credential_warning,
        )
        return MarvinClient(
            token,
            api_base_url=config.api_base_url,
            timeout_seconds=float(config.request_timeout_seconds),
            minimum_request_interval_ms=config.minimum_request_interval_ms,
        )
    except MarvinPilotError as exc:
        _fail(exc)


def _history_store(config: AppConfig) -> HistoryStore:
    return HistoryStore(effective_history_dir(config))


def _resolve_revert_source(store: HistoryStore, path: Path):
    """Resolve either an apply receipt or its exact original plan to one receipt."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanSyntaxError(f"could not read revert input {path}: {exc}") from exc
    if isinstance(raw, dict) and "receiptSchemaVersion" in raw:
        return path, store.load(path)
    plan, _source = load_plan(path)
    matches = store.find_applied_plan(plan_id=plan.planId, digest=plan_digest(plan))
    if not matches:
        raise PlanSemanticError(
            "no applied or partial receipt exactly matches this plan ID and digest"
        )
    if len(matches) > 1:
        paths = ", ".join(str(match[0]) for match in matches)
        raise PlanSemanticError(
            f"multiple apply receipts match this plan; use one directly: {paths}"
        )
    return matches[0]


@app.command("validate")
def validate_command(
    plan_path: Annotated[str, typer.Argument(help="Plan JSON path, or - for stdin.")],
) -> None:
    """Validate a change plan offline; never load a credential or access the network."""

    plan = _read_plan_argument(plan_path)
    typer.echo(
        f"Valid Marvin Pilot change plan: {len(plan.operations)} operation(s)\n"
        f"Plan ID: {plan.planId}\nDigest: {plan_digest(plan)}"
    )


@app.command("describe")
def describe_command(
    plan_path: Annotated[str, typer.Argument(help="Plan JSON path, or - for stdin.")],
    live: Annotated[
        bool,
        typer.Option("--live", help="Also check current Marvin state (requires a credential)."),
    ] = False,
    full_access_key_file: Annotated[
        Path | None,
        typer.Option(
            "--full-access-key-file",
            help="Read the secret from this file; the token itself is never a CLI argument.",
        ),
    ] = None,
) -> None:
    """Render an exact, deterministic human-readable plan diff."""

    plan = _read_plan_argument(plan_path)
    if not live:
        typer.echo(render_plan_description(plan), nl=False)
        return
    config = _load_config_or_fail()
    client = _client_from_config(config, full_access_key_file)
    try:
        result = preflight_plan(
            plan,
            client,
            now_ms=unix_milliseconds(),
            strict_concurrency=config.strict_concurrency,
        )
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        client.close()
    typer.echo(render_live_preflight(result), nl=False)


@app.command("apply")
def apply_command(
    plan_path: Annotated[str, typer.Argument(help="Plan JSON path, or - for stdin.")],
    full_access_key_file: Annotated[
        Path | None,
        typer.Option(
            "--full-access-key-file",
            help="Read the secret from this file; the token itself is never a CLI argument.",
        ),
    ] = None,
) -> None:
    """Human-review, apply, verify, and durably journal a complete plan."""

    plan, raw = _read_plan_argument_with_bytes(plan_path)
    config = _load_config_or_fail()
    if len(plan.operations) > config.max_operations:
        _fail(
            PlanSemanticError(
                f"plan has {len(plan.operations)} operations; configured maximum is "
                f"{config.max_operations}"
            )
        )
    client = _client_from_config(config, full_access_key_file)
    started = time.monotonic()

    def approve(result) -> bool:
        typer.echo(render_live_preflight(result), nl=False)
        if len(result.operations) >= config.large_plan_warning_operations:
            typer.echo(
                f"Large plan: {len(result.operations)} operations will run sequentially at "
                f"a minimum {config.minimum_request_interval_ms} ms request interval."
            )
        return confirm_apply(len(result.operations))

    def progress(current: int, total: int, operation_id: str) -> None:
        elapsed = time.monotonic() - started
        remaining = (elapsed / current) * (total - current) if current else 0
        typer.echo(
            f"Operation {current}/{total} applied [{operation_id}] "
            f"(elapsed {elapsed:.1f}s, ETA {remaining:.1f}s)",
            err=True,
        )

    try:
        result = execute_apply(
            plan,
            raw,
            client=client,
            history=_history_store(config),
            approve=approve,
            strict_concurrency=config.strict_concurrency,
            progress=progress,
        )
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        client.close()
    typer.echo(f"Applied {len(result.receipt.operations)} operation(s).")
    typer.echo(f"Receipt: {result.receipt_path}")


@app.command("revert")
def revert_command(
    receipt_path: Annotated[
        Path,
        typer.Argument(help="Applied receipt or its exact original plan JSON path."),
    ],
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help="Revert only this operation ID; repeat for multiple operations.",
        ),
    ] = None,
    full_access_key_file: Annotated[
        Path | None,
        typer.Option(
            "--full-access-key-file",
            help="Read the secret from this file; the token itself is never a CLI argument.",
        ),
    ] = None,
) -> None:
    """Human-review and revert all or selected applied operations from a receipt."""

    config = _load_config_or_fail()
    history = _history_store(config)
    try:
        resolved_receipt_path, source = _resolve_revert_source(history, receipt_path)
    except MarvinPilotError as exc:
        _fail(exc)
    selected_count = (
        len(only) if only else sum(operation.status == "applied" for operation in source.operations)
    )
    if selected_count > config.max_operations:
        _fail(
            PlanSemanticError(
                f"revert selects {selected_count} operations; configured maximum is "
                f"{config.max_operations}"
            )
        )
    client = _client_from_config(config, full_access_key_file)
    started = time.monotonic()

    def approve(result) -> bool:
        typer.echo(render_revert_preflight(result), nl=False)
        if len(result.operations) >= config.large_plan_warning_operations:
            typer.echo(
                f"Large revert: {len(result.operations)} operations will run sequentially at "
                f"a minimum {config.minimum_request_interval_ms} ms request interval."
            )
        return confirm_revert(len(result.operations))

    def progress(current: int, total: int, operation_id: str) -> None:
        elapsed = time.monotonic() - started
        remaining = (elapsed / current) * (total - current) if current else 0
        typer.echo(
            f"Operation {current}/{total} reverted [{operation_id}] "
            f"(elapsed {elapsed:.1f}s, ETA {remaining:.1f}s)",
            err=True,
        )

    try:
        result = execute_revert(
            source,
            resolved_receipt_path,
            only or [],
            client=client,
            history=history,
            approve=approve,
            strict_concurrency=config.strict_concurrency,
            progress=progress,
        )
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        client.close()
    typer.echo(f"Reverted {len(result.receipt.operations)} operation(s).")
    typer.echo(f"Receipt: {result.receipt_path}")


@app.command("schema")
def schema_command(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Create this file instead of writing to stdout."),
    ] = None,
) -> None:
    """Print the complete JSON Schema for Marvin Pilot change plans."""

    _write_or_print(plan_schema_json(), output)


@app.command("example")
def example_command(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Create this file instead of writing to stdout."),
    ] = None,
) -> None:
    """Print a complete valid update/create/trash plan."""

    content = json.dumps(EXAMPLE_PLAN, ensure_ascii=False, indent=2) + "\n"
    _write_or_print(content, output)


@help_app.command("plan-format")
def plan_format_help() -> None:
    """Explain the JSON plan contract for humans and AI assistants."""

    complete_example = json.dumps(EXAMPLE_PLAN, ensure_ascii=False, indent=2)
    content = f"""{SAFETY_CONTRACT}
PLAN FORMAT

The root is an object with schemaVersion 1, a UUID planId, an RFC 3339 createdAt timestamp
with an explicit offset, a non-empty summary, and one or more operations.

Actions:
  update  Existing task. before and after must contain identical allowlisted field sets.
  create  New task. target.id must be a caller-generated UUID and after.title is required.
  trash   Existing task. Reversible Marvin UI-style Trash; permanent deletion is unsupported.

Every operation requires a unique lowercase-hyphen operationId, a task target, and a reason.
Use operationId—not task ID—for selective revert. A single revert may repeat --only:
  marvin-pilot revert RECEIPT.json --only op-a --only op-b

Conventions:
  dates                   YYYY-MM-DD
  plannedMonth            YYYY-MM
  permanentSnoozeUntil    HH:mm
  timestamps              RFC 3339 with an explicit UTC offset
  estimatedTimeDuration   30m, 2h, or 1h30m (Marvin timeEstimate, not tracked duration)
  clear a value           JSON null
  unschedule a task       scheduledDate: null

Normal task start times are generally written into task titles. A task is not a calendar time
block. JSON comments, trailing commas, locale dates, the string "none", raw setters, credentials,
API URLs, system timestamps, completion, recurrence, reminders, calendar sync, and permanent
deletion are rejected.

Allowlisted task fields:
  title                       non-empty task title
  parent                      {{"id": "...", "title": "optional review hint"}}
  scheduledDate               YYYY-MM-DD or null to unschedule
  dueDate/startDate/endDate   YYYY-MM-DD or null
  plannedWeek                 Monday date (YYYY-MM-DD) or null
  plannedMonth                YYYY-MM or null
  labels                      array of {{"id": "...", "title": "optional hint"}} or null
  estimatedTimeDuration       duration string or null; maps to Marvin timeEstimate
  note                        string or null
  dayRank/masterRank          finite number or null
  dailySection                Morning, Afternoon, Evening, or null
  bonusSection                Essential, Bonus, or null
  customSectionId             existing section ID or null
  timeBlockSectionId          existing time-block section ID or null
  starPriority                yellow, orange, red, or null
  frogSize                    normal, baby, monster, or null
  backburner                  true, false, or null
  reviewDate                  YYYY-MM-DD or null
  snoozedUntil                RFC 3339 timestamp with offset or null
  permanentSnoozeUntil        HH:mm or null
  dependencies                array of task/project IDs or null

Generate a complete example with:
  marvin-pilot example

Generate machine-readable JSON Schema with:
  marvin-pilot schema

Review without credentials or network access with:
  marvin-pilot validate PLAN.json
  marvin-pilot describe PLAN.json

COMPLETE VERSION 1 EXAMPLE
{complete_example}
"""
    typer.echo(content)


@config_app.callback()
def config_root(ctx: typer.Context) -> None:
    """Run guided initial configuration when no config subcommand is given."""

    if ctx.invoked_subcommand is not None:
        return
    config = _load_config_or_fail()
    typer.echo(_config_summary(config))
    typer.echo("\nChoose how mutating commands obtain the full-access token.")
    mode = typer.prompt(
        "Credential mode",
        default=config.credential_mode,
    )
    if mode not in {"keyring", "prompt", "file"}:
        _fail(PlanSyntaxError("credential mode must be one of: keyring, prompt, file"))
    updates: dict[str, object] = {"credential_mode": mode}
    if mode == "file":
        updates["key_file"] = typer.prompt("Full-access key file path", default=config.key_file)
    else:
        updates["key_file"] = ""
    history_default = config.history_dir or str(default_history_dir())
    history = typer.prompt("History directory", default=history_default)
    updates["history_dir"] = "" if Path(history) == default_history_dir() else history
    try:
        updated = config.model_copy(update=updates)
        updated = AppConfig.model_validate(updated.model_dump())
        if mode == "keyring" and typer.confirm("Store the full-access token now?", default=True):
            token = typer.prompt("Amazing Marvin full-access token", hide_input=True)
            store_keyring_token(token)
        path = save_config(updated)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"Saved non-secret configuration: {path}")


@config_app.command("paths")
def config_paths() -> None:
    """Print the config and effective audit-history paths."""

    config = _load_config_or_fail()
    typer.echo(f"Config: {default_config_path()}")
    typer.echo(f"History: {effective_history_dir(config)}")


@config_app.command("show")
def config_show() -> None:
    """Show effective non-secret settings; never print token material."""

    typer.echo(_config_summary(_load_config_or_fail()))


@config_app.command("set-history-dir")
def config_set_history_dir(
    path: Annotated[Path, typer.Argument(help="Directory for apply/revert receipts.")],
) -> None:
    """Set a non-default audit-history directory."""

    config = _load_config_or_fail()
    try:
        saved_path = save_config(config.model_copy(update={"history_dir": str(path)}))
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"History directory: {path}")
    typer.echo(f"Saved: {saved_path}")


@config_app.command("set-credential-mode")
def config_set_credential_mode(
    mode: Annotated[
        str,
        typer.Argument(help="One of: keyring, prompt, file."),
    ],
    key_file: Annotated[
        Path | None,
        typer.Option("--key-file", help="Required path when selecting file mode."),
    ] = None,
) -> None:
    """Select keyring, hidden interactive prompt, or key-file credentials."""

    if mode not in {"keyring", "prompt", "file"}:
        _fail(PlanSyntaxError("credential mode must be one of: keyring, prompt, file"))
    if mode == "file" and key_file is None:
        _fail(PlanSyntaxError("--key-file is required for file credential mode"))
    if mode != "file" and key_file is not None:
        _fail(PlanSyntaxError("--key-file is accepted only for file credential mode"))
    config = _load_config_or_fail()
    updates = {"credential_mode": mode, "key_file": str(key_file or "")}
    try:
        updated = AppConfig.model_validate(config.model_copy(update=updates).model_dump())
        path = save_config(updated)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"Credential mode: {mode}")
    typer.echo(f"Saved: {path}")


@config_app.command("set-full-access-token")
def config_set_full_access_token() -> None:
    """Store the token via hidden input in the native OS keyring."""

    config = _load_config_or_fail()
    if config.credential_mode != "keyring":
        _fail(
            PlanSyntaxError(
                "set-full-access-token requires keyring mode; run "
                "'marvin-pilot config set-credential-mode keyring' first"
            )
        )
    token = typer.prompt("Amazing Marvin full-access token", hide_input=True)
    try:
        store_keyring_token(token)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo("Stored the full-access token in the native OS keyring.")


@config_app.command("unset-full-access-token")
def config_unset_full_access_token() -> None:
    """Remove Marvin Pilot's token from the native OS keyring."""

    try:
        delete_keyring_token()
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo("Removed the full-access token from the native OS keyring.")


@history_app.command("path")
def history_path() -> None:
    """Print the effective receipt-history directory."""

    config = _load_config_or_fail()
    typer.echo(str(effective_history_dir(config)))


@history_app.command("list")
def history_list() -> None:
    """List receipt status, kind, plan identity, and interrupted pending work."""

    store = _history_store(_load_config_or_fail())
    try:
        paths = store.list_paths()
        if not paths:
            typer.echo("No receipts.")
            return
        for path in paths:
            receipt = store.load(path)
            warning = (
                " [PENDING: inspect before continuing]"
                if receipt.status
                in {
                    "pending",
                    "applying",
                    "pending-revert",
                    "reverting",
                }
                else ""
            )
            typer.echo(f"{path.name}  {receipt.kind}  {receipt.status}  {receipt.planId}{warning}")
    except MarvinPilotError as exc:
        _fail(exc)


@history_app.command("show")
def history_show(
    receipt: Annotated[str, typer.Argument(help="Receipt path, or latest.")],
) -> None:
    """Print a receipt, including its reviewed plan and per-operation outcomes."""

    store = _history_store(_load_config_or_fail())
    try:
        if receipt == "latest":
            latest = store.latest()
            if latest is None:
                _fail(PlanSyntaxError("no receipts exist"))
            path, value = latest
        else:
            path = Path(receipt)
            value = store.load(path)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"Receipt: {path}")
    typer.echo(json.dumps(value.model_dump(mode="json", exclude_none=True), indent=2))


@history_app.command("verify")
def history_verify(
    receipt_path: Annotated[Path, typer.Argument(help="Receipt JSON path.")],
) -> None:
    """Validate a receipt schema and its SHA-256 content integrity."""

    store = _history_store(_load_config_or_fail())
    try:
        receipt = store.load(receipt_path)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(
        f"Valid receipt: {receipt.receiptId}\n"
        f"Kind/status: {receipt.kind}/{receipt.status}\n"
        f"Plan ID: {receipt.planId}\n"
        f"Receipt hash: {receipt.receiptHash}"
    )


def main() -> None:
    """Console-script entry point."""

    app()


if __name__ == "__main__":
    main()
