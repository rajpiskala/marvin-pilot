"""Marvin Pilot command-line interface."""

from __future__ import annotations

import json
import sys
import time
import webbrowser
from datetime import UTC, date, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
from marvin_pilot.approval import (
    confirm_apply,
    confirm_revert,
    confirm_unattended_enable,
    require_controlling_terminal,
)
from marvin_pilot.atomic import exclusive_write_bytes
from marvin_pilot.backup_cache import (
    backup_cache_dir,
    clear_backup_cache,
    load_cached_backup_documents,
)
from marvin_pilot.backup_context import (
    project_context_json,
    scheduled_day_context,
    search_backup_context,
)
from marvin_pilot.compiler import project_compiled_mutation
from marvin_pilot.config import (
    DEFAULT_UNATTENDED_MAX_IMPACT,
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
    plan_description_payload,
    render_live_preflight,
    render_plan_description,
    render_plan_markdown,
    render_revert_preflight,
)
from marvin_pilot.errors import (
    LivePreconditionError,
    MarvinPilotError,
    PlanSemanticError,
    PlanSyntaxError,
    UserDeclinedError,
)
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.executor import execute_apply, unix_milliseconds
from marvin_pilot.history import HistoryStore
from marvin_pilot.history_status import (
    audit_receipt_live,
    build_completion_day_repair_plan,
    plan_history_status,
)
from marvin_pilot.live_context import build_live_today_context
from marvin_pilot.marvin_client import MarvinClient
from marvin_pilot.models.plan_v1 import ChangePlanV1
from marvin_pilot.models.receipt_v1 import ReceiptV1
from marvin_pilot.plan_io import (
    MAX_PLAN_BYTES,
    canonical_plan_bytes,
    load_plan,
    parse_plan_bytes,
    plan_digest,
)
from marvin_pilot.plan_set import (
    LoadedPlanSet,
    PlanSetChildReceipt,
    PlanSetReceiptV1,
    ProjectedReader,
    combined_preview_plan,
    find_latest_plan_set_receipt,
    find_latest_plan_set_revert,
    load_plan_set,
    load_plan_set_receipt,
    new_plan_set_receipt,
    new_plan_set_revert_receipt,
    persist_plan_set_receipt,
    plan_set_digest,
    verify_manifest_account,
)
from marvin_pilot.preflight import LiveValidationResult, preflight_plan, validate_plan_live
from marvin_pilot.prepare import (
    SnapshotReader,
    backup_snapshot_reader,
    parse_draft_bytes,
    prepare_draft,
    prepared_plan_json,
    rebase_plan_live,
)
from marvin_pilot.reverter import execute_revert, preflight_revert
from marvin_pilot.schema import draft_schema_json, plan_schema_json, plan_set_schema_json
from marvin_pilot.terminal_review import render_live_preflight_terminal
from marvin_pilot.unattended import (
    assess_unattended,
    blocked_unattended_error,
    enforce_unattended,
    unattended_plan_blockers,
)
from marvin_pilot.visualizer import project_hierarchy_context
from marvin_pilot.visualizer_hierarchy import (
    build_backup_hierarchy_context,
    merge_hierarchy_contexts,
)
from marvin_pilot.visualizer_server import VisualizerServer

SAFETY_CONTRACT = """This CLI separates reviewed Marvin mutations from explicitly bounded
automation.
AI assistants: generate, validate, and describe plans. Invoke apply only when the user requested
the change and has enabled the local unattended policy; never invoke revert without the user.
Humans: review normal plans and run apply/revert interactively, or opt into a small unattended cap.
"""

app = typer.Typer(
    name="marvin-pilot",
    help=(
        "Safe, reversible, and policy-bounded AI actions for Amazing Marvin.\n\n" + SAFETY_CONTRACT
    ),
    no_args_is_help=True,
    rich_markup_mode=None,
)
help_app = typer.Typer(help="Detailed reference material for plan authors.")
config_app = typer.Typer(
    help="Configure non-secret settings and the native OS keyring.",
    invoke_without_command=True,
)
unattended_config_app = typer.Typer(help="Configure account-pinned bounded unattended apply.")
history_app = typer.Typer(help="Inspect and verify durable apply/revert receipts.")
contract_tests_app = typer.Typer(
    help="Generate and verify reusable, isolated contract-test plan suites."
)
context_app = typer.Typer(help="Build compact, read-only AI context from Marvin data.")
app.add_typer(help_app, name="help")
app.add_typer(config_app, name="config")
config_app.add_typer(unattended_config_app, name="unattended")
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


def _read_change_input(path: str):
    """Return (kind, parsed value, raw), accepting plans and path-based plan sets."""

    if path == "-":
        plan, raw = _read_plan_argument_with_bytes(path)
        return "plan", plan, raw
    candidate = Path(path)
    try:
        preview = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        plan, raw = _read_plan_argument_with_bytes(path)
        return "plan", plan, raw
    if isinstance(preview, dict) and "planSetVersion" in preview:
        try:
            loaded = load_plan_set(candidate)
        except MarvinPilotError as exc:
            _fail(exc)
        return "plan-set", loaded, loaded.raw
    plan, raw = _read_plan_argument_with_bytes(path)
    return "plan", plan, raw


def _render_plan_set_description(loaded: LoadedPlanSet) -> str:
    lines = [
        f"Plan set: {loaded.manifest.summary}",
        f"Plan-set ID: {loaded.manifest.planSetId}",
        f"Expected account: {loaded.manifest.expectedAccount.email} "
        f"(user ID {loaded.manifest.expectedAccount.userId})",
        f"Phases: {len(loaded.plans)}",
        "",
    ]
    for index, item in enumerate(loaded.plans, start=1):
        dependencies = ", ".join(item.entry.dependsOn) or "none"
        lines.extend(
            [
                f"PHASE {index}: {item.entry.path}",
                f"Depends on phases: {dependencies}",
                render_plan_description(item.plan).rstrip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _plan_set_description_payload(loaded: LoadedPlanSet) -> dict[str, object]:
    return {
        "kind": "plan-set",
        "planSetVersion": loaded.manifest.planSetVersion,
        "planSetId": loaded.manifest.planSetId,
        "summary": loaded.manifest.summary,
        "expectedAccount": loaded.manifest.expectedAccount.model_dump(mode="json"),
        "phases": [
            {
                "path": item.entry.path,
                "dependsOn": item.entry.dependsOn,
                "plan": plan_description_payload(item.plan),
            }
            for item in loaded.plans
        ],
    }


def _render_plan_set_markdown(loaded: LoadedPlanSet) -> str:
    lines = [
        f"# {loaded.manifest.summary}",
        "",
        f"- Plan-set ID: `{loaded.manifest.planSetId}`",
        f"- Expected account: {loaded.manifest.expectedAccount.email} "
        f"(`{loaded.manifest.expectedAccount.userId}`)",
        f"- Phases: {len(loaded.plans)}",
        "",
    ]
    for index, item in enumerate(loaded.plans, start=1):
        dependencies = ", ".join(f"`{value}`" for value in item.entry.dependsOn) or "None"
        lines.extend(
            [
                f"## Phase {index}: `{item.entry.path}`",
                "",
                f"Depends on: {dependencies}",
                "",
                render_plan_markdown(item.plan).rstrip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _format_description(
    change_input: ChangePlanV1 | LoadedPlanSet,
    input_kind: str,
    output_format: Literal["text", "markdown", "json"],
) -> str:
    if input_kind == "plan-set":
        loaded = change_input
        assert isinstance(loaded, LoadedPlanSet)
        if output_format == "text":
            return _render_plan_set_description(loaded)
        if output_format == "markdown":
            return _render_plan_set_markdown(loaded)
        return (
            json.dumps(_plan_set_description_payload(loaded), ensure_ascii=False, indent=2) + "\n"
        )
    plan = change_input
    if output_format == "text":
        return render_plan_description(plan)
    if output_format == "markdown":
        return render_plan_markdown(plan)
    return json.dumps(plan_description_payload(plan), ensure_ascii=False, indent=2) + "\n"


def _validate_plan_set_live(
    loaded: LoadedPlanSet,
    client: MarvinClient,
    *,
    strict_concurrency: bool,
) -> list[LiveValidationResult]:
    verify_manifest_account(loaded, client)
    reader = ProjectedReader(client)
    results: list[LiveValidationResult] = []
    for item in loaded.plans:
        result = validate_plan_live(
            item.plan,
            reader,
            now_ms=unix_milliseconds(),
            strict_concurrency=strict_concurrency,
        )
        results.append(result)
        if not result.valid:
            continue
        for checked in result.operations:
            reader.project(
                checked.operation.target.id,
                project_compiled_mutation(checked.live_document, checked.compiled),
            )
    return results


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


def _read_limited_argument(path: str, *, label: str) -> bytes:
    try:
        raw = sys.stdin.buffer.read(MAX_PLAN_BYTES + 1) if path == "-" else Path(path).read_bytes()
    except OSError as exc:
        _fail(PlanSyntaxError(f"could not read {label} {path}: {exc}"))
    if len(raw) > MAX_PLAN_BYTES:
        _fail(PlanSyntaxError(f"{label} exceeds the {MAX_PLAN_BYTES}-byte safety limit"))
    return raw


@app.command("prepare")
def prepare_command(
    source_path: Annotated[
        str,
        typer.Argument(help="Compact draft or full plan JSON path, or - for stdin."),
    ],
    backup: Annotated[
        Path | None,
        typer.Option("--backup", help="Local Marvin backup used for exact identity and context."),
    ] = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Use current Marvin state for exact locks and identity."),
    ] = False,
    rebase_live: Annotated[
        bool,
        typer.Option(
            "--rebase-live",
            help="Refresh only locks/generated metadata in a full plan; stop on semantic drift.",
        ),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="Create this plan file; default writes JSON to stdout."
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
    """Compile a strict compact draft, or safely rebase locks in a reviewed full plan."""

    if rebase_live and not live:
        _fail(PlanSyntaxError("--rebase-live requires --live"))
    if not live and backup is None:
        _fail(PlanSyntaxError("prepare requires --backup and/or --live"))
    raw = _read_limited_argument(source_path, label="prepare input")
    config = _load_config_or_fail() if live else None
    client = _client_from_config(config, full_access_key_file) if config is not None else None
    try:
        if backup is not None:
            reader = backup_snapshot_reader(backup, live=client)
        else:
            reader = SnapshotReader([], live=client)
        now_ms = unix_milliseconds()
        if rebase_live:
            plan = parse_plan_bytes(raw)
            assert client is not None
            prepared = rebase_plan_live(plan, reader, account_reader=client, now_ms=now_ms)
        else:
            draft = parse_draft_bytes(raw)
            if client is not None:
                # Account identity is the only read allowed before target discovery.
                account = client.check_connection()
                if (
                    account.account_user_id != draft.expectedAccount.userId
                    or account.account_email.casefold() != draft.expectedAccount.email.casefold()
                ):
                    raise LivePreconditionError(
                        "draft account mismatch: expected "
                        f"{draft.expectedAccount.email} ({draft.expectedAccount.userId}), "
                        "connected "
                        f"{account.account_email} ({account.account_user_id})"
                    )
            prepared = prepare_draft(draft, reader, now_ms=now_ms)
        content = prepared_plan_json(prepared)
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        if client is not None:
            client.close()
    _write_or_print(content, output)


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
    max_depth: Annotated[
        int | None,
        typer.Option("--max-depth", min=1, help="Return descendants only through this depth."),
    ] = None,
    state: Annotated[
        Literal["all", "open", "completed"],
        typer.Option("--state", help="Filter returned descendants by completion state."),
    ] = "all",
    since: Annotated[
        str | None,
        typer.Option("--since", help="Keep completions on/after YYYY-MM-DD."),
    ] = None,
    summary: Annotated[
        bool,
        typer.Option("--summary", help="Return counts and direct children without full items."),
    ] = False,
) -> None:
    """Return every open and completed descendant of one category/project."""

    try:
        since_date = date.fromisoformat(since) if since is not None else None
        content = project_context_json(
            backup,
            project,
            include_trash=include_trash,
            max_depth=max_depth,
            state=state,
            since=since_date,
            summary=summary,
        )
    except ValueError:
        _fail(PlanSyntaxError("--since must use YYYY-MM-DD"))
    except MarvinPilotError as exc:
        _fail(exc)
    _write_or_print(content, output)


@context_app.command("search")
def context_search_command(
    query: Annotated[str, typer.Argument(help="Title text to rank across tasks and containers.")],
    backup: Annotated[Path, typer.Option("--backup", help="Local Marvin backup.")],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Rank exact/normalized title candidates with IDs and hierarchy paths."""

    try:
        documents, _format, _size, digest, cache_hit = load_cached_backup_documents(backup)
        value = search_backup_context(documents, query)
        value["source"] = {"sha256": digest, "cacheHit": cache_hit}
    except MarvinPilotError as exc:
        _fail(exc)
    _write_or_print(json.dumps(value, ensure_ascii=False, indent=2) + "\n", output)


@context_app.command("scheduled-day")
def context_scheduled_day_command(
    day: Annotated[str, typer.Argument(help="Exact scheduled day in YYYY-MM-DD form.")],
    backup: Annotated[Path, typer.Option("--backup", help="Local Marvin backup.")],
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Return backup tasks with this exact day field (honestly not a live Today claim)."""

    try:
        parsed_day = date.fromisoformat(day)
        documents, _format, _size, digest, cache_hit = load_cached_backup_documents(backup)
        value = scheduled_day_context(documents, parsed_day)
        value["source"] = {"sha256": digest, "cacheHit": cache_hit}
    except ValueError:
        _fail(PlanSyntaxError("DAY must use YYYY-MM-DD"))
    except MarvinPilotError as exc:
        _fail(exc)
    _write_or_print(json.dumps(value, ensure_ascii=False, indent=2) + "\n", output)


@context_app.command("today")
def context_today_command(
    live: Annotated[
        bool,
        typer.Option("--live", help="Required acknowledgement: use Marvin's live Today endpoint."),
    ] = False,
    day: Annotated[
        str | None,
        typer.Option("--date", help="Today date in YYYY-MM-DD; defaults in --timezone."),
    ] = None,
    timezone: Annotated[
        str,
        typer.Option("--timezone", help="IANA timezone, e.g. America/Los_Angeles."),
    ] = "America/Los_Angeles",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    full_access_key_file: Annotated[
        Path | None,
        typer.Option(
            "--full-access-key-file",
            help="Read the secret from this file; the token itself is never a CLI argument.",
        ),
    ] = None,
) -> None:
    """Return verified live Today items, ancestors, recurrence identity, and rollover reason."""

    if not live:
        _fail(PlanSyntaxError("context today requires --live"))
    try:
        zone = ZoneInfo(timezone)
        effective_day = date.fromisoformat(day) if day is not None else datetime.now(zone).date()
    except ZoneInfoNotFoundError:
        _fail(PlanSyntaxError(f"unknown IANA timezone: {timezone}"))
    except ValueError:
        _fail(PlanSyntaxError("--date must use YYYY-MM-DD"))
    config = _load_config_or_fail()
    client = _client_from_config(config, full_access_key_file)
    try:
        value = build_live_today_context(client, effective_day)
        value["timezone"] = timezone
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        client.close()
    _write_or_print(json.dumps(value, ensure_ascii=False, indent=2) + "\n", output)


@context_app.command("backup-info")
def context_backup_info_command(
    backup: Annotated[Path, typer.Argument(help="Local Marvin .json or .json.lzma backup.")],
) -> None:
    """Hash, index, and summarize a backup without exposing its path in output."""

    try:
        documents, source_format, source_bytes, digest, cache_hit = load_cached_backup_documents(
            backup
        )
    except MarvinPilotError as exc:
        _fail(exc)
    counts: dict[str, int] = {}
    for document in documents:
        database = str(document.get("db", "unknown"))
        counts[database] = counts.get(database, 0) + 1
    typer.echo(
        json.dumps(
            {
                "sha256": digest,
                "format": source_format,
                "inputBytes": source_bytes,
                "documentCount": len(documents),
                "databases": counts,
                "cacheHit": cache_hit,
                "cachePolicy": "private content-addressed LRU; source path is not stored",
            },
            indent=2,
        )
    )


@context_app.command("cache-clear")
def context_cache_clear_command() -> None:
    """Delete Pilot's private parsed-backup cache; original backups are untouched."""

    try:
        count, size = clear_backup_cache()
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"Cleared {count} cached backup(s), {size} byte(s), from {backup_cache_dir()}.")


def _load_config_or_fail() -> AppConfig:
    try:
        return load_config()
    except MarvinPilotError as exc:
        _fail(exc)


def _config_summary(config: AppConfig) -> str:
    if config.unattended_enabled:
        unattended = (
            f"on (maximum impact {config.unattended_max_impact}, "
            f"account user ID {config.unattended_account_user_id})"
        )
    else:
        unattended = "off"
    return (
        f"Config: {default_config_path()}\n"
        f"History: {effective_history_dir(config)}\n"
        f"Credential mode: {config.credential_mode}\n"
        f"Strict concurrency: {'on' if config.strict_concurrency else 'off'}\n"
        f"Minimum request interval: {config.minimum_request_interval_ms} ms\n"
        f"Maximum operations: {config.max_operations}\n"
        f"Unattended apply: {unattended}"
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


def _resolve_plan_set_revert_source(
    store: HistoryStore, path: Path, raw: dict[str, object]
) -> tuple[Path, PlanSetReceiptV1] | None:
    """Resolve a plan-set receipt/manifest, or report that the input is not one."""

    if "planSetReceiptVersion" in raw:
        return path, load_plan_set_receipt(path)
    if "planSetVersion" not in raw:
        return None
    loaded = load_plan_set(path)
    match = find_latest_plan_set_receipt(
        store.root,
        loaded.manifest.planSetId,
        manifest_digest_value=plan_set_digest(loaded),
    )
    if match is None:
        raise PlanSemanticError(
            "no applied plan-set receipt exactly matches this manifest ID and digest"
        )
    return match


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


def _live_validation_indices(
    plan,
    *,
    from_index: int | None,
    from_operation: str | None,
    target_ids: list[str],
) -> tuple[int, ...]:
    modes = sum((from_index is not None, from_operation is not None, bool(target_ids)))
    if modes > 1:
        raise PlanSyntaxError(
            "use only one live-validation selector: --from-index, --from-operation, or --target"
        )
    if from_index is not None:
        if from_index < 1 or from_index > len(plan.operations):
            raise PlanSyntaxError(
                f"--from-index must be between 1 and {len(plan.operations)}, got {from_index}"
            )
        return tuple(range(from_index, len(plan.operations) + 1))
    if from_operation is not None:
        for index, operation in enumerate(plan.operations, start=1):
            if operation.operationId == from_operation:
                return tuple(range(index, len(plan.operations) + 1))
        raise PlanSyntaxError(f"--from-operation does not match an operation: {from_operation!r}")
    if target_ids:
        requested = set(target_ids)
        matched = tuple(
            index
            for index, operation in enumerate(plan.operations, start=1)
            if operation.target.id in requested
        )
        missing = sorted(requested - {plan.operations[index - 1].target.id for index in matched})
        if missing:
            raise PlanSyntaxError("--target ID is not present in the plan: " + ", ".join(missing))
        return matched
    return tuple(range(1, len(plan.operations) + 1))


def _live_validation_payload(result: LiveValidationResult) -> dict[str, object]:
    processed = len(result.operations) + len(result.errors)
    return {
        "schemaVersion": 1,
        "valid": result.valid,
        "planId": result.plan.planId,
        "digest": plan_digest(result.plan),
        "totalOperations": len(result.plan.operations),
        "selectedOperations": len(result.selected_indices),
        "processedOperations": processed,
        "strictConcurrency": result.strict_concurrency,
        "uniqueDocumentsChecked": result.unique_documents_checked,
        "metadataCollectionsChecked": result.metadata_collections_checked,
        "elapsedMs": result.elapsed_ms,
        "errors": len(result.errors),
        "warnings": len(result.warnings),
        "diagnostics": [
            {
                "severity": item.severity,
                "operationIndex": item.operation_index,
                "operationId": item.operation_id,
                "targetId": item.target_id,
                "check": item.check,
                "message": item.message,
                "expected": item.expected,
                "found": item.found,
            }
            for item in result.diagnostics
        ],
    }


def _render_live_validation(result: LiveValidationResult) -> str:
    status = "PASSED" if result.valid else "FAILED"
    processed = len(result.operations) + len(result.errors)
    lines = [
        f"Live validation: {status}",
        f"Plan ID: {result.plan.planId}",
        f"Digest: {plan_digest(result.plan)}",
        (
            f"Selection: {len(result.selected_indices)} of {len(result.plan.operations)} "
            f"operation(s); processed {processed}"
        ),
        f"Diagnostics: {len(result.errors)} error(s), {len(result.warnings)} warning(s)",
        (
            f"Reads: {result.unique_documents_checked} unique document(s), "
            f"{result.metadata_collections_checked} metadata collection(s); "
            f"elapsed {result.elapsed_ms / 1000:.1f}s"
        ),
    ]
    for item in result.diagnostics:
        lines.append(
            f"{item.severity.upper()} {item.operation_index}. [{item.operation_id}] "
            f"target={item.target_id} check={item.check}: {item.message}"
        )
        if item.expected is not None or item.found is not None:
            lines.append(f"  expected={item.expected!r}; found={item.found!r}")
    return "\n".join(lines) + "\n"


@app.command("validate")
def validate_command(
    plan_path: Annotated[str, typer.Argument(help="Plan JSON path, or - for stdin.")],
    live: Annotated[
        bool,
        typer.Option("--live", help="Collect read-only diagnostics against current Marvin state."),
    ] = False,
    from_index: Annotated[
        int | None,
        typer.Option("--from-index", help="Start live checks at this 1-based plan entry."),
    ] = None,
    from_operation: Annotated[
        str | None,
        typer.Option("--from-operation", help="Start live checks at this operation ID."),
    ] = None,
    target: Annotated[
        list[str] | None,
        typer.Option("--target", help="Live-check operations for this target ID; repeatable."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable JSON diagnostics; progress remains on stderr."),
    ] = False,
    fail_fast: Annotated[
        bool,
        typer.Option("--fail-fast", help="Stop live checks after the first error."),
    ] = False,
    full_access_key_file: Annotated[
        Path | None,
        typer.Option(
            "--full-access-key-file",
            help="Read the secret from this file; the token itself is never a CLI argument.",
        ),
    ] = None,
) -> None:
    """Validate offline by default, or collect read-only live diagnostics with --live."""

    input_kind, change_input, _raw = _read_change_input(plan_path)
    if input_kind == "plan-set":
        loaded: LoadedPlanSet = change_input
        live_only_options = (
            from_index is not None or from_operation is not None or bool(target) or fail_fast
        )
        if live_only_options:
            _fail(
                PlanSyntaxError(
                    "--from-index, --from-operation, --target, and --fail-fast currently select "
                    "entries only in a single plan"
                )
            )
        if not live:
            total = sum(len(item.plan.operations) for item in loaded.plans)
            if json_output:
                typer.echo(
                    json.dumps(
                        {
                            "valid": True,
                            "kind": "plan-set",
                            "planSetId": loaded.manifest.planSetId,
                            "phases": len(loaded.plans),
                            "operations": total,
                        },
                        indent=2,
                    )
                )
            else:
                typer.echo(
                    f"Valid Marvin Pilot plan set: {len(loaded.plans)} phase(s), "
                    f"{total} operation(s)\nPlan-set ID: {loaded.manifest.planSetId}"
                )
            return
        config = _load_config_or_fail()
        client = _client_from_config(config, full_access_key_file)
        try:
            results = _validate_plan_set_live(
                loaded, client, strict_concurrency=config.strict_concurrency
            )
        except MarvinPilotError as exc:
            _fail(exc)
        finally:
            client.close()
        valid = all(result.valid for result in results)
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "valid": valid,
                        "kind": "plan-set",
                        "planSetId": loaded.manifest.planSetId,
                        "phases": [
                            {
                                "path": item.entry.path,
                                "result": _live_validation_payload(result),
                            }
                            for item, result in zip(loaded.plans, results, strict=True)
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            typer.echo(
                f"Live plan-set validation: {'PASSED' if valid else 'FAILED'}\n",
                nl=False,
            )
            for item, result in zip(loaded.plans, results, strict=True):
                typer.echo(
                    f"\nPhase: {item.entry.path}\n{_render_live_validation(result)}", nl=False
                )
        if not valid:
            raise typer.Exit(code=LivePreconditionError.exit_code)
        return
    plan = change_input
    live_only_options = (
        from_index is not None
        or from_operation is not None
        or bool(target)
        or json_output
        or fail_fast
        or full_access_key_file is not None
    )
    if not live:
        if live_only_options:
            _fail(PlanSyntaxError("live-validation options require --live"))
        typer.echo(
            f"Valid Marvin Pilot change plan: {len(plan.operations)} operation(s)\n"
            f"Plan ID: {plan.planId}\nDigest: {plan_digest(plan)}"
        )
        return
    try:
        selected_indices = _live_validation_indices(
            plan,
            from_index=from_index,
            from_operation=from_operation,
            target_ids=target or [],
        )
    except MarvinPilotError as exc:
        _fail(exc)
    config = _load_config_or_fail()
    client = _client_from_config(config, full_access_key_file)
    display = _OperationProgress("Live validation", len(selected_indices))
    display.start()
    try:
        result = validate_plan_live(
            plan,
            client,
            now_ms=unix_milliseconds(),
            strict_concurrency=config.strict_concurrency,
            selected_indices=selected_indices,
            fail_fast=fail_fast,
            progress=display.update,
        )
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        display.stop()
        client.close()
    if json_output:
        typer.echo(json.dumps(_live_validation_payload(result), ensure_ascii=False, indent=2))
    else:
        typer.echo(_render_live_validation(result), nl=False)
    if not result.valid:
        raise typer.Exit(code=LivePreconditionError.exit_code)


@app.command("describe")
def describe_command(
    plan_path: Annotated[str, typer.Argument(help="Plan JSON path, or - for stdin.")],
    output_format: Annotated[
        Literal["text", "markdown", "json"],
        typer.Option("--format", help="Description format for humans or tooling."),
    ] = "text",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Create this file instead of writing to stdout."),
    ] = None,
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
    """Render an exact deterministic plan diff as text, Markdown, or JSON."""

    input_kind, change_input, _raw = _read_change_input(plan_path)
    base_description = _format_description(change_input, input_kind, output_format)
    if input_kind == "plan-set":
        loaded: LoadedPlanSet = change_input
        if not live:
            _write_or_print(base_description, output)
            return
        config = _load_config_or_fail()
        client = _client_from_config(config, full_access_key_file)
        try:
            results = _validate_plan_set_live(
                loaded, client, strict_concurrency=config.strict_concurrency
            )
        except MarvinPilotError as exc:
            _fail(exc)
        finally:
            client.close()
        if any(not result.valid for result in results):
            typer.echo(base_description, nl=False)
            for item, result in zip(loaded.plans, results, strict=True):
                typer.echo(
                    f"\nPhase: {item.entry.path}\n{_render_live_validation(result)}", nl=False
                )
            raise typer.Exit(code=LivePreconditionError.exit_code)
        if output_format == "json":
            value = _plan_set_description_payload(loaded)
            value["live"] = {
                "valid": True,
                "phases": [
                    {
                        "path": item.entry.path,
                        "result": _live_validation_payload(result),
                    }
                    for item, result in zip(loaded.plans, results, strict=True)
                ],
            }
            content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        elif output_format == "markdown":
            content = base_description + "\n## Live preflight\n\nPassed.\n"
        else:
            content = base_description + "\nLive plan-set preflight: PASSED\n"
        _write_or_print(content, output)
        return
    plan = change_input
    if not live:
        _write_or_print(base_description, output)
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
    if output_format == "json":
        value = plan_description_payload(plan)
        value["live"] = {
            "valid": True,
            "strictConcurrency": result.strict_concurrency,
            "warnings": [
                {
                    "operationIndex": warning.operation_index,
                    "operationId": warning.operation_id,
                    "message": warning.message,
                }
                for warning in result.warnings
            ],
        }
        content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    elif output_format == "markdown":
        warning_lines = [f"- {warning.message}" for warning in result.warnings]
        content = base_description + "\n## Live preflight\n\nPassed.\n"
        if warning_lines:
            content += "\n### Warnings\n\n" + "\n".join(warning_lines) + "\n"
    else:
        content = render_live_preflight(result)
    _write_or_print(content, output)


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
    backup: Annotated[
        Path | None,
        typer.Option(
            "--backup",
            help=(
                "Locally resolve omitted hierarchy from a Marvin .json or .json.lzma backup; "
                "the plan remains unchanged."
            ),
        ),
    ] = None,
    receipt: Annotated[
        Path | None,
        typer.Option(
            "--receipt",
            help=(
                "Verify an exact applied receipt and use its pre-apply documents for the "
                "Before view. Requires PLAN."
            ),
        ),
    ] = None,
    context_plan: Annotated[
        list[Path] | None,
        typer.Option(
            "--context-plan",
            help=(
                "Project an earlier prerequisite plan before rendering PLAN; repeat in "
                "dependency order."
            ),
        ),
    ] = None,
) -> None:
    """Open an offline, credential-free, read-only browser preview."""

    plan = None
    loaded_plan_set: LoadedPlanSet | None = None
    source_name = None
    if plan_path is not None:
        input_kind, change_input, _raw = _read_change_input(plan_path)
        if input_kind == "plan-set":
            loaded_plan_set = change_input
            plan = combined_preview_plan(loaded_plan_set)
        else:
            plan = change_input
        source_name = "stdin" if plan_path == "-" else Path(plan_path).name
    hierarchy = None
    hierarchy_sources: list[str] = []
    if backup is not None:
        try:
            documents, _source_format, _source_bytes, _digest, _cache_hit = (
                load_cached_backup_documents(backup)
            )
            hierarchy = build_backup_hierarchy_context(documents)
            hierarchy_sources.append("local backup")
        except MarvinPilotError as exc:
            _fail(exc)
    for prerequisite_path in context_plan or []:
        try:
            prerequisite, _source = load_plan(prerequisite_path)
            hierarchy = project_hierarchy_context(prerequisite, hierarchy)
            hierarchy_sources.append("prerequisite plan projection")
        except MarvinPilotError as exc:
            _fail(exc)
    if loaded_plan_set is not None:
        hierarchy_sources.append(
            f"plan-set projection ({len(loaded_plan_set.plans)} dependency-ordered phases)"
        )
    verified_receipt = None
    if receipt is not None:
        if loaded_plan_set is not None:
            _fail(PlanSemanticError("--receipt currently requires a single plan, not a plan set"))
        if plan is None:
            _fail(PlanSemanticError("--receipt requires a preselected PLAN"))
        try:
            verified_receipt = HistoryStore(receipt.parent).load(receipt)
        except MarvinPilotError as exc:
            _fail(exc)
        assert plan is not None
        if verified_receipt.kind != "apply" or verified_receipt.status != "applied":
            _fail(PlanSemanticError("--receipt requires a fully applied apply receipt"))
        if verified_receipt.planId != plan.planId or verified_receipt.planDigest != plan_digest(
            plan
        ):
            _fail(
                PlanSemanticError(
                    "receipt does not exactly match the selected plan ID and canonical digest"
                )
            )
        receipt_documents = [
            operation.beforeDocument
            for operation in verified_receipt.operations
            if operation.beforeDocument is not None and operation.status == "applied"
        ]
        receipt_hierarchy = build_backup_hierarchy_context(receipt_documents)
        hierarchy = merge_hierarchy_contexts(hierarchy, receipt_hierarchy)
        hierarchy_sources.append("verified apply receipt")
    server = VisualizerServer(
        preloaded_plan=plan,
        source_name=source_name,
        hierarchy=hierarchy,
        hierarchy_sources=tuple(hierarchy_sources),
        review_state="applied" if verified_receipt is not None else "preview",
        receipt_id=verified_receipt.receiptId if verified_receipt is not None else None,
    )
    typer.echo(f"Visualizer: {server.url}")
    if verified_receipt is not None:
        typer.echo(f"Applied plan — verified receipt {verified_receipt.receiptId}.")
    else:
        typer.echo("Preview only — nothing has been applied.")
    if hierarchy is not None:
        source_summary = (
            "the backup" if hierarchy_sources == ["local backup"] else " + ".join(hierarchy_sources)
        )
        typer.echo(
            f"Hierarchy: {len(hierarchy.nodes)} active item(s) loaded locally from "
            f"{source_summary}."
        )
    elif plan is not None:
        omitted_paths = sum(
            operation.display is None or f"{side}Path" not in operation.display.model_fields_set
            for operation in plan.operations
            for side in ("before", "after")
            if not (side == "before" and operation.action == "create")
            and not (side == "after" and operation.action == "trash")
        )
        if omitted_paths:
            typer.echo(
                f"Hierarchy note: {omitted_paths} visible state(s) omit typed paths. "
                "Use --backup BACKUP.json.lzma for local hierarchy resolution."
            )
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


def _apply_loaded_plan_set(
    loaded: LoadedPlanSet,
    *,
    full_access_key_file: Path | None,
    yes: bool,
    unattended: bool,
) -> None:
    """Apply dependency-ordered child plans with one approval and one parent receipt."""

    config = _load_config_or_fail()
    total = sum(len(item.plan.operations) for item in loaded.plans)
    if total > config.max_operations:
        _fail(
            PlanSemanticError(
                f"plan set has {total} operations; configured maximum is {config.max_operations}"
            )
        )
    combined = combined_preview_plan(loaded)
    if unattended:
        if not config.unattended_enabled:
            _fail(
                PlanSemanticError(
                    "unattended apply is disabled; enable it with 'marvin-pilot config "
                    "unattended enable --max-impact 10'"
                )
            )
        if loaded.manifest.expectedAccount.userId != config.unattended_account_user_id:
            _fail(PlanSemanticError("plan-set account does not match the unattended policy"))
        blockers = unattended_plan_blockers(combined)
        if blockers:
            _fail(blocked_unattended_error(blockers))
        assessment = assess_unattended(combined)
        if assessment.impact > config.unattended_max_impact:
            _fail(
                PlanSemanticError(
                    f"plan-set impact is {assessment.impact}; configured unattended maximum is "
                    f"{config.unattended_max_impact}"
                )
            )
    history = _history_store(config)
    prior = find_latest_plan_set_receipt(history.root, loaded.manifest.planSetId)
    if prior is not None and prior[1].status in {"applying", "applied", "partial"}:
        _fail(
            PlanSemanticError(f"plan set was already recorded as {prior[1].status!r} in {prior[0]}")
        )
    client = _client_from_config(config, full_access_key_file)
    try:
        results = _validate_plan_set_live(
            loaded, client, strict_concurrency=config.strict_concurrency
        )
        errors = [diagnostic for result in results for diagnostic in result.errors]
        if errors:
            details = "\n".join(f"  [{item.operation_id}] {item.message}" for item in errors)
            raise LivePreconditionError(
                f"plan-set live preflight found {len(errors)} error(s):\n{details}"
            )
        console.print(_render_plan_set_description(loaded), markup=False)
        if unattended:
            approved = True
            console.print("[bold bright_green]UNATTENDED PLAN-SET APPLY AUTHORIZED[/]")
        elif yes:
            approved = True
            console.print("[bold bright_yellow]PROMPT SKIPPED[/] Explicit --yes/-y supplied.")
        else:
            approved = confirm_apply(total)
        if not approved:
            raise UserDeclinedError("apply declined; no Marvin changes were made")

        parent = new_plan_set_receipt(loaded)
        parent_path = persist_plan_set_receipt(history.root, parent)
        completed = 0
        try:
            for phase_index, item in enumerate(loaded.plans, start=1):
                console.print(
                    f"Applying phase {phase_index}/{len(loaded.plans)}: {item.entry.path}",
                    markup=False,
                )
                child_result = execute_apply(
                    item.plan,
                    item.raw,
                    client=client,
                    history=history,
                    approve=lambda _result: True,
                    strict_concurrency=config.strict_concurrency,
                )
                parent.children.append(
                    PlanSetChildReceipt(
                        planPath=item.entry.path,
                        planId=item.plan.planId,
                        planDigest=plan_digest(item.plan),
                        receiptPath=child_result.receipt_path,
                        status=child_result.receipt.status,
                    )
                )
                completed += 1
                persist_plan_set_receipt(history.root, parent)
        except MarvinPilotError:
            parent.status = "partial"
            parent.endedAt = (
                datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            persist_plan_set_receipt(history.root, parent)
            raise
        parent.status = "applied"
        parent.endedAt = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        parent_path = persist_plan_set_receipt(history.root, parent)
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        client.close()
    console.print(f"[bold bright_green]Applied:[/] {completed} phase(s), {total} operation(s)")
    console.print(f"[bold bright_cyan]Plan-set receipt:[/] {parent_path}", soft_wrap=True)


def _revert_plan_set(
    source_path: Path,
    source: PlanSetReceiptV1,
    *,
    only: list[str],
    config: AppConfig,
    history: HistoryStore,
    full_access_key_file: Path | None,
) -> None:
    """Preflight and reverse a plan set's child receipts in reverse phase order."""

    if source.kind != "apply" or source.status not in {"applied", "partial"}:
        _fail(PlanSemanticError("plan-set revert requires an applied or partial apply receipt"))
    directory = history.root / "plan-sets"
    if directory.exists():
        for candidate in directory.glob("*.json"):
            receipt = load_plan_set_receipt(candidate)
            if (
                receipt.kind == "revert"
                and receipt.sourcePlanSetReceiptId == source.receiptId
                and receipt.status in {"reverting", "reverted", "partial"}
            ):
                _fail(
                    PlanSemanticError(f"plan set is already claimed by revert receipt {candidate}")
                )

    children: list[tuple[PlanSetChildReceipt, Path, ReceiptV1]] = []
    for child in reversed(source.children):
        child_path = Path(child.receiptPath)
        if not child_path.is_absolute():
            child_path = (source_path.parent / child_path).resolve()
        children.append((child, child_path, history.load(child_path)))
    available = {
        operation.operationId
        for _child, _path, receipt in children
        for operation in receipt.operations
        if operation.status == "applied"
    }
    if len(only) != len(set(only)):
        _fail(PlanSemanticError("each --only operation ID may be provided at most once"))
    unknown = sorted(set(only) - available)
    if unknown:
        _fail(PlanSemanticError("unknown applied operation ID(s): " + ", ".join(unknown)))
    selected = set(only) if only else available
    phase_selections = [
        (
            child,
            path,
            receipt,
            [
                operation.operationId
                for operation in receipt.operations
                if operation.status == "applied" and operation.operationId in selected
            ],
        )
        for child, path, receipt in children
    ]
    phase_selections = [item for item in phase_selections if item[3]]
    total = sum(len(item[3]) for item in phase_selections)
    if total == 0:
        _fail(PlanSemanticError("plan-set receipt has no selected applied operations"))
    if total > config.max_operations:
        _fail(
            PlanSemanticError(
                f"plan-set revert selects {total} operations; configured maximum is "
                f"{config.max_operations}"
            )
        )

    client = _client_from_config(config, full_access_key_file)
    projected = ProjectedReader(client)
    display = _OperationProgress("Plan-set revert preflight", total)
    display.start()
    completed_preflight = 0
    try:
        account = client.check_connection()
        if (
            account.account_user_id != source.accountUserId
            or account.account_email.casefold() != source.accountEmail.casefold()
        ):
            raise LivePreconditionError(
                f"plan-set receipt account mismatch: expected {source.accountEmail} "
                f"({source.accountUserId}), connected {account.account_email} "
                f"({account.account_user_id})"
            )
        for _child, child_path, receipt, operation_ids in phase_selections:
            checked = preflight_revert(
                receipt,
                child_path,
                operation_ids,
                client=projected,
                history=history,
                now_ms=unix_milliseconds(),
                strict_concurrency=config.strict_concurrency,
                progress=lambda current, _total, operation_id, base=completed_preflight: (
                    display.update(base + current, total, operation_id)
                ),
            )
            for operation in checked.operations:
                projected.project(
                    operation.compiled.target_id,
                    project_compiled_mutation(operation.live_document, operation.compiled),
                )
            completed_preflight += len(checked.operations)
        display.stop()
        typer.echo(
            f"Plan-set revert: {source.planSetId}\n"
            f"Phases: {len(phase_selections)} (reverse dependency order)\n"
            f"Operations: {total}\n"
            f"Expected account: {source.accountEmail} ({source.accountUserId})\n"
        )
        if not confirm_revert(total):
            raise UserDeclinedError("revert declined; no Marvin changes were made")

        parent = new_plan_set_revert_receipt(source, source_path)
        parent_path = persist_plan_set_receipt(history.root, parent)
        try:
            for phase_index, (child, child_path, receipt, operation_ids) in enumerate(
                phase_selections, start=1
            ):
                console.print(
                    f"Reverting phase {phase_index}/{len(phase_selections)}: {child.planPath}",
                    markup=False,
                )
                result = execute_revert(
                    receipt,
                    child_path,
                    operation_ids,
                    client=client,
                    history=history,
                    approve=lambda _result: True,
                    strict_concurrency=config.strict_concurrency,
                )
                parent.children.append(
                    PlanSetChildReceipt(
                        planPath=child.planPath,
                        planId=child.planId,
                        planDigest=child.planDigest,
                        receiptPath=result.receipt_path,
                        status=result.receipt.status,
                    )
                )
                persist_plan_set_receipt(history.root, parent)
        except MarvinPilotError:
            parent.status = "partial"
            parent.endedAt = (
                datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            persist_plan_set_receipt(history.root, parent)
            raise
        parent.status = "reverted"
        parent.endedAt = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        parent_path = persist_plan_set_receipt(history.root, parent)
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        display.stop()
        client.close()
    console.print(f"[bold bright_green]Reverted:[/] {total} operation(s)")
    console.print(f"[bold bright_cyan]Plan-set receipt:[/] {parent_path}", soft_wrap=True)


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
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help=(
                "Skip the final explicit y/n prompt after successful preflight; an interactive "
                "controlling terminal is still required."
            ),
        ),
    ] = False,
    unattended: Annotated[
        bool,
        typer.Option(
            "--unattended",
            help=(
                "Apply without a terminal or prompt only when the account-pinned local policy "
                "is enabled and this plan is within its impact limit."
            ),
        ),
    ] = False,
) -> None:
    """Preflight, apply, verify, and durably journal a complete plan."""

    if yes and unattended:
        _fail(PlanSyntaxError("use either --yes or --unattended, not both"))
    if yes:
        try:
            require_controlling_terminal()
        except MarvinPilotError as exc:
            _fail(exc)
    input_kind, change_input, raw = _read_change_input(plan_path)
    if input_kind == "plan-set":
        _apply_loaded_plan_set(
            change_input,
            full_access_key_file=full_access_key_file,
            yes=yes,
            unattended=unattended,
        )
        return
    plan = change_input
    config = _load_config_or_fail()
    if unattended and not config.unattended_enabled:
        _fail(
            PlanSemanticError(
                "unattended apply is disabled; a human can enable it with "
                "'marvin-pilot config unattended enable --max-impact 10'"
            )
        )
    if len(plan.operations) > config.max_operations:
        _fail(
            PlanSemanticError(
                f"plan has {len(plan.operations)} operations; configured maximum is "
                f"{config.max_operations}"
            )
        )
    if unattended:
        blockers = unattended_plan_blockers(plan)
        if blockers:
            _fail(blocked_unattended_error(blockers))
        if len(plan.operations) > config.unattended_max_impact:
            _fail(
                PlanSemanticError(
                    f"plan has at least {len(plan.operations)} impact from its operations; "
                    "configured unattended maximum is "
                    f"{config.unattended_max_impact}"
                )
            )
        if plan.expectedAccount is None:
            _fail(
                PlanSemanticError(
                    "unattended apply requires expectedAccount.userId and expectedAccount.email "
                    "in the plan"
                )
            )
        if plan.expectedAccount.userId != config.unattended_account_user_id:
            _fail(
                PlanSemanticError(
                    "plan expectedAccount does not match the account pinned by the unattended "
                    "policy"
                )
            )
    client = _client_from_config(config, full_access_key_file)
    unattended_account_email: str | None = None
    if unattended:
        try:
            account = client.check_connection()
        except MarvinPilotError as exc:
            client.close()
            _fail(exc)
        if account.account_user_id != config.unattended_account_user_id:
            client.close()
            _fail(
                PlanSemanticError(
                    "unattended apply account mismatch: configured user ID "
                    f"{config.unattended_account_user_id!r}, connected user ID "
                    f"{account.account_user_id!r}; re-enable unattended apply intentionally "
                    "for this account"
                )
            )
        unattended_account_email = account.account_email
    preflight_display = _OperationProgress("Preflight", len(plan.operations))
    apply_display: _OperationProgress | None = None
    preflight_display.start()

    def approve(result) -> bool:
        nonlocal apply_display
        preflight_display.stop()
        assessment = assess_unattended(result)
        if unattended:
            assessment = enforce_unattended(
                result,
                max_impact=config.unattended_max_impact,
            )
        console.print(render_live_preflight_terminal(result, encoding=console.encoding or "utf-8"))
        execution_notes = Text()
        if unattended:
            execution_notes.append("Authorization  ", style="bold bright_green")
            execution_notes.append(
                f"Unattended policy for {unattended_account_email} "
                f"(impact {assessment.impact}/{config.unattended_max_impact})\n"
            )
        execution_notes.append("Network path  ", style="bold bright_cyan")
        execution_notes.append(
            "live recheck -> mutation/response verification "
            f"(minimum {config.minimum_request_interval_ms} ms between requests)"
        )
        has_trash = any(operation.operation.action == "trash" for operation in result.operations)
        if has_trash:
            execution_notes.append("\nTRASH safety  ", style="bold bright_red")
            execution_notes.append(
                "Trash recovery: Pilot will delete each document through Marvin's API after "
                "durably storing its full recovery snapshot. It will not appear in Marvin's "
                "native Trash; keep the receipt to revert it."
            )
        if len(result.operations) >= config.large_plan_warning_operations:
            execution_notes.append("\nLarge plan  ", style="bold bright_yellow")
            execution_notes.append(f"{len(result.operations)} operations will run sequentially.")
        if (
            not unattended
            and not config.unattended_enabled
            and assessment.eligible
            and assessment.impact <= DEFAULT_UNATTENDED_MAX_IMPACT
        ):
            execution_notes.append("\nTip  ", style="bold bright_cyan")
            execution_notes.append(
                f"This small plan has impact {assessment.impact}. To let an AI apply future "
                "plans this small without a prompt, a human can run: marvin-pilot config "
                f"unattended enable --max-impact {DEFAULT_UNATTENDED_MAX_IMPACT}"
            )
        console.print(
            Panel(
                execution_notes,
                title="EXECUTION DETAILS",
                border_style="bright_yellow",
                box=box.ASCII,
                padding=(1, 2),
            )
        )
        if unattended:
            console.print(
                "[bold bright_green]UNATTENDED APPLY AUTHORIZED[/] "
                "by the account-pinned local policy."
            )
            approved = True
        elif yes:
            console.print(
                "[bold bright_yellow]PROMPT SKIPPED[/] "
                "Explicit --yes/-y supplied in an interactive terminal."
            )
            approved = True
        else:
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
    applied_text = Text()
    applied_text.append("Applied:", style="bold bright_green")
    applied_text.append(f" {len(result.receipt.operations)} operation(s)")
    console.print(applied_text)
    receipt_text = Text()
    receipt_text.append("Receipt:", style="bold bright_cyan")
    receipt_text.append(f" {result.receipt_path}")
    console.print(receipt_text, soft_wrap=True)


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
    """Human-review and revert a plan/plan-set receipt in reverse dependency order."""

    config = _load_config_or_fail()
    history = _history_store(config)
    try:
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        plan_set_source = (
            _resolve_plan_set_revert_source(history, receipt_path, raw)
            if isinstance(raw, dict)
            else None
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(PlanSyntaxError(f"could not read revert input {receipt_path}: {exc}"))
    except MarvinPilotError as exc:
        _fail(exc)
    if plan_set_source is not None:
        resolved_path, source_set = plan_set_source
        _revert_plan_set(
            resolved_path,
            source_set,
            only=only or [],
            config=config,
            history=history,
            full_access_key_file=full_access_key_file,
        )
        return
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
    kind: Annotated[
        Literal["plan", "draft", "plan-set"],
        typer.Option("--kind", help="Export the full plan, compact draft, or plan-set schema."),
    ] = "plan",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Create this file instead of writing to stdout."),
    ] = None,
) -> None:
    """Print a strict JSON Schema for plans, compact drafts, or plan sets."""

    content = {
        "plan": plan_schema_json,
        "draft": draft_schema_json,
        "plan-set": plan_set_schema_json,
    }[kind]()
    _write_or_print(content, output)


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
with an explicit offset, a non-empty summary, and one or more operations. Pin live execution with
expectedAccount: {{"userId":"numeric Marvin ID","email":"account@example.com"}}. Pilot checks
that identity before document discovery and again immediately before writes. Unattended plans
require it; reviewed legacy plans may omit it.

Actions:
  update    Existing task, project, category, recurrence series, or generated occurrence.
  create    New task, project, category, or recurrence series using a caller-generated UUID.
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

Ordered same-target operations:
  - Prefer one update when fields can be changed atomically. When actions cannot be coalesced
    (for example rename -> move -> complete), repeat the target in plan order.
  - Every later same-target operation must depend directly on the immediately preceding one.
    Its target.title must be the title produced by that step, and it must omit expectedUpdatedAt.
  - Only the first operation consumes the original expectedUpdatedAt lock. Live validation
    simulates successful intermediate state; apply and reverse-order revert use actual post-write
    revisions for strict concurrency checks. Trash is terminal.
  - Preview shows the item's initial and final states once with ordered-step badges. Changes keeps
    every intermediate operation visible.
  - A newly created item's fields belong in its one create operation. Pilot rejects later
    same-target operations after create as avoidable/contradictory lifecycle work.

Normal task start times are generally written into task titles. A task is not a calendar time
block. JSON comments, trailing commas, locale dates, the string "none", raw setters, credentials,
API URLs, raw completion fields, implicit recurrence scope, reminders, and calendar sync are
rejected. The trash action uses Marvin's deletion endpoint only after Pilot durably journals the
full original document; recovery is through `marvin-pilot revert`, not Marvin's native Trash UI.

Allowlisted task/project fields:
  title                       non-empty item title
  parent                      {{"id": "...", "title": "optional warning-only review hint"}}
  scheduledDate               YYYY-MM-DD or null to unschedule
  dueDate/startDate/endDate   YYYY-MM-DD or null
  plannedWeek                 Monday date (YYYY-MM-DD) or null
  plannedMonth                YYYY-MM or null
  labels                      array of {{"id": "...", "title": "optional hint"}} or null;
                              stale titles warn but do not block
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
  orbit                       true or false; include/exclude the task from Orbit

Relative sibling order (update operations only):
  "siblingOrder": {{"beforeId":"sibling-id"}}
  "siblingOrder": {{"afterId":"sibling-id"}}
  "siblingOrder": {{"position":"first"}}  (or "last")
Pilot resolves this to Marvin's day rank for scheduled tasks, master rank for unscheduled tasks,
and container rank for projects/categories during live preflight. The anchor must be a compatible
sibling in the final day/parent. Do not combine siblingOrder with raw dayRank/masterRank changes.

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
    depend on the parent create/update operation.
  - Marvin ordering/history bookkeeping (rank, masterRank, firstScheduled, workedOnAt) is expected
    to disappear. Meaningful unrepresentable fields require an exact sourceTask.acceptLoss array.
    Allowed acknowledgments: scheduledDate, dueDate, startDate, endDate, plannedWeek, plannedMonth,
    labels, estimatedTimeDuration, note, dailySection, bonusSection, customSectionId,
    timeBlockSectionId, starPriority, frogSize, backburner, reviewDate, snoozedUntil,
    permanentSnoozeUntil. Missing and unused acknowledgments fail live validation.
  - Existing source subtasks, dependencies, recurrence, completion, and tracked-time history remain
    hard blockers. Accepted losses are visible in descriptions, live/apply warnings, visualizer
    badges, and the receipt's immutable source plan.
  - Pilot merges retained native subtask records by ID, preserves unknown native metadata, writes
    deterministic ranks, snapshots the entire embedded map, and restores it exactly on revert.

Container notes:
  - Use target.type "project" or "category". Both live in Marvin's Categories database with
    their corresponding type. Categories support create, rename, move, relative order, and Trash;
    they cannot be completed and their editable fields are title and parent.
  - A task or project may use a project created earlier in the same plan as its parent. Put the
    parent create operation first and list its operationId in dependsOnOperations.
  - Container moves are rejected if the proposed ancestry would create or inherit a parent cycle.
    Project/category Trash is blocked unless the documented /children read proves every direct
    child was already moved or deleted, including earlier projected operations in this plan.
  - complete.completedAt may not be later than the plan's createdAt. Pilot writes exact historical
    doneAt and assigns day to the local YYYY-MM-DD encoded by completedAt for both tasks and
    projects; projects also receive doneDate. A prepared plan adds completionDay
    {{before,after,behavior}} so that history-bucket replacement is explicit during review. Native
    completion field-update timestamps and updatedAt remain the actual apply time.
  - For a legacy project completed by an older Pilot version without doneAt, history audit --live
    --repair-plan may emit complete with repairHistory: true. Live preflight requires the project
    to remain completed, requires doneDate to match completedAt, and refuses to overwrite any
    existing doneAt.
  - Revert restores prior open/completed fields. Reverting create deletes the created document;
    reverting trash recreates the exact ID from Pilot's full-document recovery snapshot. These
    deleted documents do not appear in Marvin's native Trash UI, so retain private receipts.
  - Apply verifies the full task/project document and receipts say explicitly that server
    /doneItems visibility was not checked. Audit through full-document reads. For older Pilot
    receipts, history audit --live detects missing/wrong task history days and missing project
    completion timestamps; --repair-plan PATH emits a separately reviewable locked plan without
    applying it.

Generate a complete example with:
  marvin-pilot example

Generate machine-readable JSON Schema with:
  marvin-pilot schema
  marvin-pilot schema --kind draft
  marvin-pilot schema --kind plan-set

Review without credentials or network access with:
  marvin-pilot validate PLAN.json
  marvin-pilot describe PLAN.json
  marvin-pilot describe PLAN.json --format markdown -o REVIEW.md
  marvin-pilot describe PLAN.json --format json

Collect every read-only live error/warning before human apply with:
  marvin-pilot validate PLAN.json --live
  marvin-pilot validate PLAN.json --live --json
  marvin-pilot validate PLAN.json --live --from-index 190

Required target, recurrence-series, and sourceTask titles are strict safety hints. Optional
parent/label titles produce review warnings with the exact live value; they do not replace ID and
updatedAt checks. Apply always revalidates the complete plan regardless of diagnostic selectors.

COMPACT AUTHORING

`marvin-pilot prepare` accepts a closed draftVersion 1 document. A draft retains only intent:
expectedAccount, summary, operationId/action/target/reason/dependencies, desired after fields,
completion time, and optional siblingOrder. Existing targets may use an exact ID, or a unique
exact/normalized backup title. Pilot fills target titles, before-state, updatedAt locks,
caller-safe create UUIDs, and typed display paths from a local backup and optional live overlay.
It never invents desired dates, parents, titles, or actions.

  marvin-pilot prepare draft.json --backup MarvinBackup.json.lzma -o plan.json
  marvin-pilot prepare draft.json --backup MarvinBackup.json.lzma --live -o plan.json
  marvin-pilot prepare reviewed-plan.json --live --rebase-live -o rebased-plan.json

Use --rebase-live only for a previously reviewed full plan: it creates a new plan ID and refreshes
locks/generated paths only when every reviewed semantic before-value still matches. Any drift
requires a new review. Input - reads stdin; omitted -o writes strict plan JSON to stdout.

PLAN SETS

A planSetVersion 1 manifest applies dependency-ordered plan files with one review and a parent
receipt. Paths are safe and relative to the manifest. Every child plan must have the same exact
expectedAccount, and operation IDs must be unique across the set:

  {{
    "planSetVersion": 1,
    "planSetId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "summary": "Apply structure before cleanup.",
    "expectedAccount": {{"userId":"123456","email":"account@example.com"}},
    "plans": [
      {{"path":"01-structure.json"}},
      {{"path":"02-cleanup.json","dependsOn":["01-structure.json"]}}
    ]
  }}

validate, describe, visualize, apply, history status, and revert accept this manifest. Live
validation projects each successful phase over one shared document cache. Apply runs phases
forward; revert uses child receipts in reverse dependency order. Parent and child receipts remain
integrity checked. --only operation-id can select operations across a plan-set revert.

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


@config_app.command("set-max-operations")
def config_set_max_operations(
    maximum: Annotated[
        int,
        typer.Argument(min=1, max=500, help="Maximum operations accepted by apply or revert."),
    ],
) -> None:
    """Set the general reviewed apply/revert operation ceiling."""

    config = _load_config_or_fail()
    updates = {
        "max_operations": maximum,
        "large_plan_warning_operations": min(config.large_plan_warning_operations, maximum),
    }
    updated = AppConfig.model_validate(config.model_copy(update=updates).model_dump())
    try:
        path = save_config(updated)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(f"Maximum operations: {maximum}")
    typer.echo(f"Saved: {path}")


@unattended_config_app.command("enable")
def config_unattended_enable(
    max_impact: Annotated[
        int,
        typer.Option(
            "--max-impact",
            min=1,
            max=500,
            help="Maximum explicit document/subtask impact for an unattended plan.",
        ),
    ] = DEFAULT_UNATTENDED_MAX_IMPACT,
    full_access_key_file: Annotated[
        Path | None,
        typer.Option(
            "--full-access-key-file",
            help="Read the secret from this file; the token itself is never a CLI argument.",
        ),
    ] = None,
) -> None:
    """Verify one account and enable bounded, receipt-backed unattended apply."""

    config = _load_config_or_fail()
    client = _client_from_config(config, full_access_key_file)
    try:
        account = client.check_connection()
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        client.close()
    console.print(
        f"Verified account: [bold]{account.account_email}[/] (user ID {account.account_user_id})"
    )
    console.print(
        "Unattended apply retains full live preflight, concurrency checks, durable receipts, "
        "and write verification. Project Trash and recurring-series changes remain interactive."
    )
    try:
        approved = confirm_unattended_enable(account.account_email, max_impact)
    except MarvinPilotError as exc:
        _fail(exc)
    if not approved:
        _fail(UserDeclinedError("unattended apply remains disabled; no configuration changed"))
    updated = AppConfig.model_validate(
        config.model_copy(
            update={
                "unattended_enabled": True,
                "unattended_max_impact": max_impact,
                "unattended_account_user_id": account.account_user_id,
            }
        ).model_dump()
    )
    try:
        path = save_config(updated)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo(
        f"Unattended apply enabled for user ID {account.account_user_id} "
        f"with maximum impact {max_impact}."
    )
    typer.echo(f"Saved: {path}")


@unattended_config_app.command("disable")
def config_unattended_disable() -> None:
    """Disable unattended apply and remove its pinned account ID."""

    config = _load_config_or_fail()
    updated = AppConfig.model_validate(
        config.model_copy(
            update={
                "unattended_enabled": False,
                "unattended_account_user_id": "",
            }
        ).model_dump()
    )
    try:
        path = save_config(updated)
    except MarvinPilotError as exc:
        _fail(exc)
    typer.echo("Unattended apply disabled.")
    typer.echo(f"Saved: {path}")


@unattended_config_app.command("show")
def config_unattended_show() -> None:
    """Show the effective non-secret unattended apply policy."""

    config = _load_config_or_fail()
    typer.echo(f"Enabled: {'yes' if config.unattended_enabled else 'no'}")
    typer.echo(f"Maximum impact: {config.unattended_max_impact}")
    account_id = config.unattended_account_user_id or "not pinned"
    typer.echo(f"Account user ID: {account_id}")


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


@history_app.command("status")
def history_status_command(
    source: Annotated[
        Path,
        typer.Argument(help="Plan, plan-set manifest, or directory to inspect locally."),
    ],
    recursive: Annotated[
        bool,
        typer.Option("--recursive", "-r", help="Scan JSON files below a directory recursively."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable JSON."),
    ] = False,
) -> None:
    """Report receipt-backed applied/reverted status without Marvin API access."""

    history = _history_store(_load_config_or_fail())
    if source.is_dir():
        paths = sorted(source.rglob("*.json") if recursive else source.glob("*.json"))
    else:
        paths = [source]
    results: list[dict[str, object]] = []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "planSetVersion" in raw:
                loaded = load_plan_set(path)
                match = find_latest_plan_set_receipt(
                    history.root,
                    loaded.manifest.planSetId,
                    manifest_digest_value=plan_set_digest(loaded),
                )
                revert = (
                    find_latest_plan_set_revert(history.root, match[1].receiptId)
                    if match is not None
                    else None
                )
                if revert is not None and revert[1].status == "reverted":
                    set_status = "reverted"
                elif revert is not None:
                    set_status = f"{revert[1].status}-revert"
                else:
                    set_status = match[1].status if match is not None else "never-applied"
                results.append(
                    {
                        "path": str(path),
                        "kind": "plan-set",
                        "id": loaded.manifest.planSetId,
                        "status": set_status,
                        "receiptPath": (
                            str(revert[0])
                            if revert is not None
                            else (str(match[0]) if match is not None else None)
                        ),
                    }
                )
            elif isinstance(raw, dict) and "schemaVersion" in raw:
                plan, _plan_raw = load_plan(path)
                status = plan_history_status(plan, history)
                results.append(
                    {
                        "path": str(path),
                        "kind": "plan",
                        "id": status.plan_id,
                        "status": status.status,
                        "receiptPath": status.receipt_path,
                        "details": status.details,
                    }
                )
        except MarvinPilotError as exc:
            results.append({"path": str(path), "kind": "invalid", "error": str(exc)})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if not source.is_dir():
                _fail(PlanSyntaxError(f"could not inspect {path}: {exc}"))
    if json_output:
        typer.echo(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        return
    if not results:
        typer.echo("No plan or plan-set JSON files found.")
        return
    for result in results:
        status = result.get("status", "invalid")
        typer.echo(f"{status}: {result['path']}")
        if result.get("receiptPath"):
            typer.echo(f"  receipt: {result['receiptPath']}")
        if result.get("error"):
            typer.echo(f"  error: {result['error']}")


@history_app.command("audit")
def history_audit_command(
    receipt_path: Annotated[
        Path,
        typer.Argument(help="Apply receipt or plan-set parent receipt to audit."),
    ],
    live: Annotated[
        bool,
        typer.Option("--live", help="Required acknowledgement: read current Marvin documents."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit stable machine-readable JSON."),
    ] = False,
    repair_plan: Annotated[
        Path | None,
        typer.Option(
            "--repair-plan",
            help=(
                "Write a review-only plan for task history-day or missing project timestamp "
                "defects proven by the receipt and current full documents; refuses to "
                "overwrite an existing file."
            ),
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
    """Compare live documents with a receipt; optionally draft safe completion repairs."""

    if not live:
        _fail(PlanSyntaxError("history audit requires --live"))
    config = _load_config_or_fail()
    history = _history_store(config)
    client = _client_from_config(config, full_access_key_file)
    try:
        raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        batches: list[tuple[str, list[dict[str, object]]]] = []
        expected_account: dict[str, str] | None = None
        if isinstance(raw, dict) and "planSetReceiptVersion" in raw:
            parent = load_plan_set_receipt(receipt_path)
            if parent.kind != "apply":
                raise PlanSemanticError("history audit requires an apply plan-set receipt")
            account = client.check_connection()
            if (
                account.account_user_id != parent.accountUserId
                or account.account_email.casefold() != parent.accountEmail.casefold()
            ):
                raise LivePreconditionError(
                    f"plan-set receipt account mismatch: expected {parent.accountEmail} "
                    f"({parent.accountUserId}), connected {account.account_email} "
                    f"({account.account_user_id})"
                )
            expected_account = {
                "userId": parent.accountUserId,
                "email": parent.accountEmail,
            }
            for child in parent.children:
                child_path = Path(child.receiptPath)
                if not child_path.is_absolute():
                    child_path = (receipt_path.parent / child_path).resolve()
                receipt = HistoryStore(child_path.parent).load(child_path)
                batches.append((child.planPath, audit_receipt_live(receipt, client)))
        else:
            receipt = history.load(receipt_path)
            batches.append((receipt_path.name, audit_receipt_live(receipt, client)))
            if receipt.accountUserId is not None and receipt.accountEmail is not None:
                expected_account = {
                    "userId": receipt.accountUserId,
                    "email": receipt.accountEmail,
                }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(PlanSyntaxError(f"could not read receipt {receipt_path}: {exc}"))
    except MarvinPilotError as exc:
        _fail(exc)
    finally:
        client.close()
    counts: dict[str, int] = {}
    for _name, items in batches:
        for item in items:
            state = str(item["state"])
            counts[state] = counts.get(state, 0) + 1
    repair_result: dict[str, object] | None = None
    if repair_plan is not None:
        if expected_account is None:
            _fail(
                PlanSemanticError(
                    "cannot build a repair plan because the receipt does not pin an account"
                )
            )
        try:
            repair = build_completion_day_repair_plan(
                [item for _name, items in batches for item in items],
                expected_account=expected_account,
            )
            exclusive_write_bytes(repair_plan, canonical_plan_bytes(repair) + b"\n")
            repair_result = {
                "path": str(repair_plan),
                "operations": len(repair.operations),
                "digest": plan_digest(repair),
            }
        except FileExistsError:
            _fail(PlanSyntaxError(f"refusing to overwrite existing repair plan: {repair_plan}"))
        except MarvinPilotError as exc:
            _fail(exc)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "receipt": str(receipt_path),
                    "counts": counts,
                    "batches": [{"name": name, "operations": items} for name, items in batches],
                    "repairPlan": repair_result,
                    "note": "Marvin UI caches may lag; this audit reads full documents.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    typer.echo("Live receipt audit (full-document reads):")
    for name, items in batches:
        typer.echo(f"\n{name}")
        for item in items:
            title = item.get("targetTitle") or item["targetId"]
            typer.echo(f"  {item['state']}: {title} [{item['operationId']}] — {item['detail']}")
    if repair_result is not None:
        typer.echo(
            f"\nRepair plan: {repair_result['path']} "
            f"({repair_result['operations']} operation(s), {repair_result['digest']})"
        )
        typer.echo("Review it with validate, describe, and visualize before applying it.")
    typer.echo("\nNote: Marvin UI caches may lag behind these document reads.")


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
