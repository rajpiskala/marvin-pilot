"""Marvin Pilot command-line interface."""

from __future__ import annotations

import json
import sys
import time
import webbrowser
from datetime import UTC, date, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from marvin_pilot import __version__
from marvin_pilot.approval import confirm_apply, confirm_revert
from marvin_pilot.backup_context import project_context_json
from marvin_pilot.config import (
    AppConfig,
    default_config_path,
    default_history_dir,
    effective_history_dir,
    load_config,
    save_config,
)
from marvin_pilot.contract_tests import (
    account_config_schema_json,
    generate_contract_suite,
    load_account_config,
    verify_contract_suite,
    write_contract_suite,
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
from marvin_pilot.errors import (
    MarvinPilotError,
    PlanSemanticError,
    PlanSyntaxError,
    UserDeclinedError,
)
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.executor import execute_apply, unix_milliseconds
from marvin_pilot.history import HistoryStore
from marvin_pilot.marvin_client import MarvinClient
from marvin_pilot.plan_io import MAX_PLAN_BYTES, load_plan, parse_plan_bytes, plan_digest
from marvin_pilot.preflight import preflight_plan
from marvin_pilot.reverter import execute_revert
from marvin_pilot.schema import plan_schema_json
from marvin_pilot.visualizer_server import VisualizerServer

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
contract_tests_app = typer.Typer(
    help="Generate and verify reusable, isolated contract-test plan suites."
)
context_app = typer.Typer(help="Build compact, read-only AI context from Marvin data.")
app.add_typer(help_app, name="help")
app.add_typer(config_app, name="config")
app.add_typer(history_app, name="history")
app.add_typer(contract_tests_app, name="contract-tests")
app.add_typer(context_app, name="context")
console = Console(stderr=False)
error_console = Console(stderr=True)


class _OperationProgress:
    """One compact terminal line for a network-heavy operation phase."""

    def __init__(self, phase: str, total: int) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            TextColumn("{task.fields[operation]}", markup=False),
            console=error_console,
            transient=False,
        )
        self._task_id = self._progress.add_task(
            phase,
            total=total,
            operation="Waiting for Marvin…",
        )
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._progress.start()
            self._started = True

    def update(self, current: int, _total: int, operation_id: str) -> None:
        self._progress.update(
            self._task_id,
            completed=current,
            operation=operation_id,
            refresh=True,
        )

    def stop(self) -> None:
        if self._started:
            self._progress.stop()
            self._started = False


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
    """Safe, reviewed task and project changes for Amazing Marvin."""


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


@context_app.command("project")
def context_project_command(
    project: Annotated[
        str,
        typer.Argument(help="Exact category/project title or Marvin document ID."),
    ],
    backup: Annotated[
        Path,
        typer.Option(
            "--backup",
            help="Marvin .json or .json.lzma backup; read locally without API access.",
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Create this private JSON file instead of writing context to stdout.",
        ),
    ] = None,
    include_trash: Annotated[
        bool,
        typer.Option(
            "--include-trash",
            help="Include descendants currently in Marvin Trash; excluded by default.",
        ),
    ] = False,
) -> None:
    """Return every open and completed descendant of one category/project."""

    try:
        content = project_context_json(backup, project, include_trash=include_trash)
    except MarvinPilotError as exc:
        _fail(exc)
    _write_or_print(content, output)


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


def _build_client(config: AppConfig, key_file: Path | None) -> MarvinClient:
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


def _client_from_config(config: AppConfig, key_file: Path | None) -> MarvinClient:
    try:
        return _build_client(config, key_file)
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


def _doctor_http_description(status_code: int | None, reason_phrase: str = "") -> str:
    if status_code is None:
        return "No HTTP response"
    if not reason_phrase:
        try:
            reason_phrase = HTTPStatus(status_code).phrase
        except ValueError:
            reason_phrase = "Unknown status"
    return f"{status_code} {reason_phrase}"


def _doctor_http_state(status_code: int | None) -> str:
    if status_code is not None and 200 <= status_code < 300:
        return "ok"
    if status_code == 429 or (status_code is not None and 300 <= status_code < 400):
        return "warn"
    return "fail"


def _doctor_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1_000:.0f} ms"
    return f"{seconds:.2f} s"


def _render_doctor_report(
    rows: list[tuple[str, str, str]],
    *,
    success: bool,
    target_console: Console,
) -> None:
    styles = {
        "ok": ("PASS", "bold green"),
        "warn": ("WARN", "bold yellow"),
        "fail": ("FAIL", "bold red"),
        "info": ("INFO", "cyan"),
    }
    table = Table.grid(padding=(0, 2))
    table.add_column(width=4)
    table.add_column(style="bold")
    table.add_column()
    for state, label, detail in rows:
        symbol, style = styles[state]
        table.add_row(
            Text(symbol, style=style),
            Text(label),
            Text(detail, style=style),
        )
    result_style = "bold green" if success else "bold red"
    result = "All checks passed" if success else "Doctor found a problem"
    table.add_row(
        Text("PASS" if success else "FAIL", style=result_style),
        Text("Result"),
        Text(result, style=result_style),
    )
    target_console.print(
        Panel.fit(
            table,
            title=Text("Marvin Pilot Doctor", style="bold cyan"),
            border_style="green" if success else "red",
            box=box.ASCII,
            padding=(1, 2),
        )
    )


@app.command("doctor")
def doctor_command(
    full_access_key_file: Annotated[
        Path | None,
        typer.Option(
            "--full-access-key-file",
            help="Read the secret from this file; the token itself is never a CLI argument.",
        ),
    ] = None,
) -> None:
    """Show account identity, HTTP status, and full-access API health without writes."""

    rows: list[tuple[str, str, str]] = []
    try:
        config = load_config()
    except MarvinPilotError as exc:
        rows.extend(
            [
                ("fail", "Configuration", str(exc)),
                ("ok", "Safety", "No Marvin data was changed"),
            ]
        )
        _render_doctor_report(rows, success=False, target_console=error_console)
        raise typer.Exit(exc.exit_code) from exc

    rows.append(("ok", "Configuration", f"{config.credential_mode} credential mode"))
    try:
        client = _build_client(config, full_access_key_file)
    except MarvinPilotError as exc:
        rows.extend(
            [
                ("fail", "Credential", str(exc)),
                ("ok", "Safety", "No Marvin data was changed"),
            ]
        )
        _render_doctor_report(rows, success=False, target_console=error_console)
        raise typer.Exit(exc.exit_code) from exc

    rows.append(("ok", "Credential", "Full-access token loaded securely"))
    started = time.perf_counter()
    try:
        with console.status("[cyan]Contacting Amazing Marvin…[/cyan]", spinner="dots"):
            check = client.check_connection()
    except MarvinPilotError as exc:
        elapsed = time.perf_counter() - started
        rows.extend(
            [
                ("fail", "Request", f"GET {client.api_base_url}/me"),
                (
                    _doctor_http_state(exc.http_status_code),
                    "HTTP status",
                    _doctor_http_description(exc.http_status_code),
                ),
                ("info", "Response time", _doctor_elapsed(elapsed)),
                ("fail", "Diagnosis", str(exc)),
                ("ok", "Safety", "Read-only check; no Marvin data was changed"),
            ]
        )
        _render_doctor_report(rows, success=False, target_console=error_console)
        raise typer.Exit(exc.exit_code) from exc
    finally:
        client.close()

    elapsed = time.perf_counter() - started
    rows.extend(
        [
            ("ok", "Account", check.account_email),
            ("ok", "User ID", check.account_user_id),
            ("ok", "Request", f"{check.method} {check.request_url}"),
            (
                _doctor_http_state(check.status_code),
                "HTTP status",
                _doctor_http_description(check.status_code, check.reason_phrase),
            ),
            ("info", "Response time", _doctor_elapsed(elapsed)),
            ("ok", "Safety", "Read-only check; no Marvin data was changed"),
        ]
    )
    _render_doctor_report(rows, success=True, target_console=console)


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
    preflight_display = _OperationProgress("Preflight", len(plan.operations))
    preflight_display.start()
    try:
        result = preflight_plan(
            plan,
            client,
            now_ms=unix_milliseconds(),
            strict_concurrency=config.strict_concurrency,
            progress=preflight_display.update,
        )
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        preflight_display.stop()
        client.close()
    typer.echo(render_live_preflight(result), nl=False)


@app.command("visualize")
def visualize_command(
    plan_path: Annotated[
        str | None,
        typer.Argument(help="Optional plan JSON path, or - for stdin."),
    ] = None,
    no_open: Annotated[
        bool,
        typer.Option("--no-open", help="Print the local URL without opening a browser."),
    ] = False,
) -> None:
    """Open an offline, credential-free, read-only browser preview."""

    plan = None
    source_name = None
    if plan_path is not None:
        plan = _read_plan_argument(plan_path)
        source_name = "stdin" if plan_path == "-" else Path(plan_path).name
    server = VisualizerServer(preloaded_plan=plan, source_name=source_name)
    typer.echo(f"Visualizer: {server.url}")
    typer.echo("Preview only — nothing has been applied.")
    typer.echo("No Marvin credential or API connection is used. Press Ctrl+C to stop.")
    if not no_open:
        try:
            opened = webbrowser.open(server.url)
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            typer.echo("Could not open a browser automatically; use the URL above.", err=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("\nVisualizer stopped.")
    finally:
        server.shutdown()


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
    preflight_display = _OperationProgress("Preflight", len(plan.operations))
    apply_display: _OperationProgress | None = None
    preflight_display.start()

    def approve(result) -> bool:
        nonlocal apply_display
        preflight_display.stop()
        typer.echo(render_live_preflight(result), nl=False)
        typer.echo(
            "Apply network path: live recheck → mutation/response verification "
            f"(minimum {config.minimum_request_interval_ms} ms between requests)."
        )
        if any(operation.operation.action == "trash" for operation in result.operations):
            typer.echo(
                "Trash recovery: Pilot will delete each document through Marvin's API after "
                "durably storing its full recovery snapshot. It will not appear in Marvin's "
                "native Trash; keep the receipt to revert it."
            )
        if len(result.operations) >= config.large_plan_warning_operations:
            typer.echo(f"Large plan: {len(result.operations)} operations will run sequentially.")
        approved = confirm_apply(len(result.operations))
        if approved:
            apply_display = _OperationProgress("Apply", len(result.operations))
            apply_display.start()
        return approved

    def preflight_progress(current: int, total: int, operation_id: str) -> None:
        preflight_display.update(current, total, operation_id)

    def progress(current: int, total: int, operation_id: str) -> None:
        if apply_display is not None:
            apply_display.update(current, total, operation_id)

    try:
        result = execute_apply(
            plan,
            raw,
            client=client,
            history=_history_store(config),
            approve=approve,
            strict_concurrency=config.strict_concurrency,
            preflight_progress=preflight_progress,
            progress=progress,
        )
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        preflight_display.stop()
        if apply_display is not None:
            apply_display.stop()
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
    preflight_display = _OperationProgress("Revert preflight", selected_count)
    revert_display: _OperationProgress | None = None
    preflight_display.start()

    def approve(result) -> bool:
        nonlocal revert_display
        preflight_display.stop()
        typer.echo(render_revert_preflight(result), nl=False)
        typer.echo(
            "Revert network path: live recheck → mutation/response verification "
            f"(minimum {config.minimum_request_interval_ms} ms between requests)."
        )
        if len(result.operations) >= config.large_plan_warning_operations:
            typer.echo(f"Large revert: {len(result.operations)} operations will run sequentially.")
        approved = confirm_revert(len(result.operations))
        if approved:
            revert_display = _OperationProgress("Revert", len(result.operations))
            revert_display.start()
        return approved

    def preflight_progress(current: int, total: int, operation_id: str) -> None:
        preflight_display.update(current, total, operation_id)

    def progress(current: int, total: int, operation_id: str) -> None:
        if revert_display is not None:
            revert_display.update(current, total, operation_id)

    try:
        result = execute_revert(
            source,
            resolved_receipt_path,
            only or [],
            client=client,
            history=history,
            approve=approve,
            strict_concurrency=config.strict_concurrency,
            preflight_progress=preflight_progress,
            progress=progress,
        )
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        preflight_display.stop()
        if revert_display is not None:
            revert_display.stop()
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
    """Print a complete valid task-reorganization plan."""

    content = json.dumps(EXAMPLE_PLAN, ensure_ascii=False, indent=2) + "\n"
    _write_or_print(content, output)


@contract_tests_app.command("generate")
def contract_tests_generate_command(
    output: Annotated[
        Path,
        typer.Argument(help="New or empty directory for generated plans and manifest."),
    ],
    base_date: Annotated[
        str,
        typer.Option(
            "--base-date",
            help="Date used to derive future schedule/date fixtures (YYYY-MM-DD).",
        ),
    ],
    account_config: Annotated[
        Path | None,
        typer.Option(
            "--account-config",
            help="Optional account-reference JSON for parent/label/section/non-task coverage.",
        ),
    ] = None,
    run_id: Annotated[
        UUID | None,
        typer.Option(
            "--run-id",
            help="Reproduce deterministic IDs; default creates a fresh isolated run UUID.",
        ),
    ] = None,
    created_at: Annotated[
        str | None,
        typer.Option(
            "--created-at",
            help="Reproducible RFC 3339 plan timestamp; default is current UTC.",
        ),
    ] = None,
    scale_count: Annotated[
        int,
        typer.Option("--scale-count", min=1, max=500, help="Scale create operation count."),
    ] = 200,
    include_limit_cases: Annotated[
        bool,
        typer.Option(
            "--include-limit-cases",
            help="Also generate offline-only 500-valid and 501-invalid boundary plans.",
        ),
    ] = False,
) -> None:
    """Generate an offline-validated, isolated live contract-test suite."""

    try:
        try:
            effective_base_date = date.fromisoformat(base_date)
        except ValueError as exc:
            raise PlanSyntaxError("--base-date must use YYYY-MM-DD") from exc
        config = load_account_config(account_config)
        effective_run_id = run_id or uuid4()
        effective_created_at = created_at or datetime.now(UTC).isoformat()
        suite = generate_contract_suite(
            config,
            run_id=effective_run_id,
            created_at=effective_created_at,
            base_date=effective_base_date,
            scale_count=scale_count,
            include_limit_cases=include_limit_cases,
        )
        write_contract_suite(output, suite)
        valid, invalid = verify_contract_suite(output)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"Contract suite: {output}")
    typer.echo(f"Run ID: {effective_run_id}")
    typer.echo(f"Verified: {valid} valid case(s), {invalid} expected-invalid case(s)")
    if config.sampleOnly:
        typer.echo("WARNING: account config marks this suite sample-only; do not apply it.")
    missing = [name for name, enabled in suite.manifest["coverage"].items() if not enabled]
    if missing:
        typer.echo("Optional live coverage not configured: " + ", ".join(missing))
    typer.echo("Read manifest.json and the contract-test runbook before any live apply.")


@contract_tests_app.command("verify")
def contract_tests_verify_command(
    suite_path: Annotated[
        Path,
        typer.Argument(help="Generated contract-suite directory containing manifest.json."),
    ],
) -> None:
    """Verify suite hashes and every expected offline-valid/invalid case."""

    try:
        valid, invalid = verify_contract_suite(suite_path)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"Valid contract suite: {valid} valid case(s), {invalid} expected-invalid case(s)")


@contract_tests_app.command("account-schema")
def contract_tests_account_schema_command(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Create this file instead of writing to stdout."),
    ] = None,
) -> None:
    """Print the JSON Schema for optional account-reference configuration."""

    _write_or_print(account_config_schema_json(), output)


@help_app.command("plan-format")
def plan_format_help() -> None:
    """Explain the JSON plan contract for humans and AI assistants."""

    complete_example = json.dumps(EXAMPLE_PLAN, ensure_ascii=False, indent=2)
    content = f"""{SAFETY_CONTRACT}
PLAN FORMAT

The root is an object with schemaVersion 1, a UUID planId, an RFC 3339 createdAt timestamp
with an explicit offset, a non-empty summary, and one or more operations.

Actions:
  update    Existing task, project, recurrence series, or explicit generated occurrence.
  create    New task, project, or recurrence series using a caller-generated UUID.
  complete  Existing task/project or explicit occurrence; a series itself cannot be completed.
  trash     Delete an existing item, occurrence, or series after journaling its recovery snapshot.

Every operation requires a unique lowercase-hyphen operationId, a typed target, and a reason.
Use operationId—not the task/project ID—for selective revert. A single revert may repeat --only:
  marvin-pilot revert RECEIPT.json --only op-a --only op-b

Optional review-only Preview metadata:
  reviewDisplay.showDaySectionsByDefault   recommend Today grouping for this plan
  display.beforePath / afterPath           typed ancestor arrays for each state
  display.beforeOrder / afterOrder         optional target sibling order for each state
  display.beforeDaySection                 visible Today section {{key,title,order}}
  display.afterDaySection                  visible Today section {{key,title,order}}
  display.beforeSection / afterSection     legacy untyped context fallback
  display.existingCompletedAt              original completion time for a done task update/Trash
Path nodes use {{id,type,title}} with type inbox, category, project, or task; optional emoji,
#RRGGBB color, and integer order are display-only. Paths contain ancestors only, never the target
itself. An empty path means a known root; omitted/null paths render as location not supplied.
For complete, an omitted afterPath inherits a known beforePath because completion does not move
the item; explicit afterPath: null remains unknown. The same rule applies to omitted afterOrder.
Day sections are Today-view grouping, not category ancestry, and may have custom titles. The
closed review objects are included in the plan digest but never compile to Marvin setters.
Updating an already-completed task requires its exact RFC 3339 display.existingCompletedAt.
Live preflight compares it with Marvin's doneAt, and the update preserves done/doneAt unchanged.

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
API URLs, raw completion fields, implicit recurrence scope, reminders, and calendar sync are
rejected. The trash action uses Marvin's deletion endpoint only after Pilot durably journals the
full original document; recovery is through `marvin-pilot revert`, not Marvin's native Trash UI.

Allowlisted task/project fields:
  title                       non-empty item title
  parent                      {{"id": "...", "title": "optional review hint"}}
  scheduledDate               YYYY-MM-DD or null to unschedule
  dueDate/startDate/endDate   YYYY-MM-DD or null
  plannedWeek                 Monday date (YYYY-MM-DD) or null
  plannedMonth                YYYY-MM or null
  labels                      array of {{"id": "...", "title": "optional hint"}} or null
  estimatedTimeDuration       duration string or null; maps to Marvin timeEstimate
  note                        string or null
  subtasks                    ordered array of {{id,title,done?,sourceTask?}} or null; tasks only
  dayRank                     finite number or null
  masterRank                  finite number or null; tasks only
  dailySection                Morning, Afternoon, Evening, or null
  bonusSection                Essential, Bonus, or null
  customSectionId             existing section ID or null
  timeBlockSectionId          existing time-block section ID or null
  starPriority                yellow, orange, red, or null; tasks only
  frogSize                    normal, baby, monster, or null
  backburner                  true, false, or null
  reviewDate                  YYYY-MM-DD or null
  snoozedUntil                RFC 3339 timestamp with offset or null
  permanentSnoozeUntil        HH:mm or null
  dependencies                array of task/project IDs or null; tasks only

Recurrence targets and fields:
  - An entire recurring-task series uses target.type "recurringTask". Create requires after.title
    and an explicit after.cadence. Series updates may use title, parent, labels,
    estimatedTimeDuration, note, ordered subtasks {{id,title}}, starPriority, frogSize,
    dueInDays, and cadence. JSON null clears every supported series field except cadence.
  - One generated occurrence remains target.type "task" and must add:
      "recurrence": {{"scope":"occurrence","seriesId":"...","seriesTitle":"...",
                       "scheduledDate":"YYYY-MM-DD"}}
    Preflight verifies recurring=true, recurringTaskId, the current occurrence day, and the live
    RecurringTasks template before allowing update, complete, or Trash.
  - A series edit changes the template used for future generation. Already-generated occurrences
    are separate Tasks; include an explicit operation for each one that must also change.
  - Series subtasks compile to Marvin's ordered subtaskList and are copied into newly generated
    occurrences. Generated occurrences use normal ordered task subtasks.
  - Individual echo/repeat-after-completion occurrences remain blocked because completing or
    deleting one generates another task. Echo series templates can still be reviewed explicitly.

Cadence forms (every form requires startDate and accepts endDate: YYYY-MM-DD or null):
  daily         {{"type":"daily","startDate":"YYYY-MM-DD"}}
  weekly        add weekday (0=Sunday ... 6=Saturday)
  monthly       add monthDate (1..31), optional limitToWeekdays
  n per week    add unique weekdays array
  repeat        type repeat/repeat week/repeat month/repeat year; add interval >= 1
  echo          add daysAfterCompletion >= 1
  onOff         add onDays and offDays >= 1
  custom        add non-empty human-readable expression

Subtask notes:
  - Array order is Marvin subtask order. IDs are stable subtask identities; omission removes a
    prior subtask, and null clears the checklist. Title and done support rename/reopen/complete.
  - To consolidate a loose task, add sourceTask: {{"id":"...","title":"..."}} only on a new
    after.subtasks item, then add a later trash operation for that source task. The Trash must
    depend on the parent create/update operation. Live preflight rejects stale, coupled, completed,
    or metadata-rich sources that a basic subtask cannot preserve.
  - Pilot merges retained native subtask records by ID, preserves unknown native metadata, writes
    deterministic ranks, snapshots the entire embedded map, and restores it exactly on revert.

Project notes:
  - Use target.type "project". Pilot stores projects in Marvin's Categories database with
    type "project"; it does not create categories.
  - A task or project may use a project created earlier in the same plan as its parent. Put the
    parent create operation first and list its operationId in dependsOnOperations.
  - Project moves are rejected if the proposed ancestry would create or inherit a parent cycle.
  - complete.completedAt may not be later than the plan's createdAt. Pilot writes a task's
    historical doneAt or the local YYYY-MM-DD doneDate required by a project, plus Marvin's
    matching historical completion field-update timestamps. updatedAt remains the actual apply
    time so concurrency checks stay truthful.
  - Revert restores prior open/completed fields. Reverting create deletes the created document;
    reverting trash recreates the exact ID from Pilot's full-document recovery snapshot. These
    deleted documents do not appear in Marvin's native Trash UI, so retain private receipts.
  - Marvin's server-maintained /doneItems history does not index backdated /doc/update task
    completions. Audit those completion dates through full-document reads and Pilot receipts.

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


@history_app.command("finalize-interrupted")
def history_finalize_interrupted(
    receipt_path: Annotated[Path, typer.Argument(help="Interrupted pending receipt path.")],
) -> None:
    """Finalize a stopped journal whose persisted states prove no send is ambiguous."""

    store = _history_store(_load_config_or_fail())
    try:
        receipt = store.load(receipt_path)
        ambiguous = sum(
            operation.status in {"sending", "verifying", "unknown", "reverting"}
            for operation in receipt.operations
        )
        completed_status = "applied" if receipt.kind == "apply" else "reverted"
        completed = sum(
            operation.status in {completed_status, "already-reverted"}
            for operation in receipt.operations
        )
        typer.echo(f"Interrupted receipt: {receipt_path}")
        typer.echo(f"Kind/status: {receipt.kind}/{receipt.status}")
        typer.echo(f"Confirmed completed operations: {completed}")
        typer.echo(f"Ambiguous send states: {ambiguous}")
        typer.echo("This command makes no Marvin API call and does not retry an operation.")
        if not typer.confirm(
            "Finalize this journal after confirming its original process has stopped?",
            default=False,
        ):
            raise UserDeclinedError("interrupted receipt finalization declined")
        destination = store.finalize_interrupted(receipt_path)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"Finalized receipt: {destination}")


def main() -> None:
    """Console-script entry point."""

    app()


if __name__ == "__main__":
    main()
