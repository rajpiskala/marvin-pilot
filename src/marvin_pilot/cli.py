"""Marvin Pilot command-line interface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from marvin_pilot import __version__
from marvin_pilot.describe import render_plan_description
from marvin_pilot.errors import MarvinPilotError, PlanSyntaxError
from marvin_pilot.examples import EXAMPLE_PLAN
from marvin_pilot.plan_io import MAX_PLAN_BYTES, load_plan, parse_plan_bytes, plan_digest
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
app.add_typer(help_app, name="help")
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


def _read_plan_argument(path: str):
    try:
        if path == "-":
            return parse_plan_bytes(sys.stdin.buffer.read(MAX_PLAN_BYTES + 1))
        return load_plan(Path(path))[0]
    except MarvinPilotError as exc:
        _fail(exc)


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
) -> None:
    """Render an exact, deterministic human-readable plan diff."""

    if live:
        _fail(PlanSyntaxError("--live will be enabled with the credentialed preflight milestone"))
    plan = _read_plan_argument(plan_path)
    typer.echo(render_plan_description(plan), nl=False)


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

Generate a complete example with:
  marvin-pilot example

Generate machine-readable JSON Schema with:
  marvin-pilot schema

Review without credentials or network access with:
  marvin-pilot validate PLAN.json
  marvin-pilot describe PLAN.json
"""
    typer.echo(content)


def main() -> None:
    """Console-script entry point."""

    app()


if __name__ == "__main__":
    main()
