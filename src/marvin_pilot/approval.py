"""One concise approval prompt on a real terminal, including for piped plans."""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from typing import TextIO

from marvin_pilot.errors import PlanSyntaxError


def _open_controlling_terminal(stack: ExitStack) -> tuple[TextIO, TextIO]:
    try:
        if os.name == "nt":
            input_stream = stack.enter_context(open("CONIN$", encoding="utf-8"))  # noqa: SIM115
            output_stream = stack.enter_context(
                open("CONOUT$", "w", encoding="utf-8")  # noqa: SIM115
            )
        else:
            input_stream = stack.enter_context(open("/dev/tty", encoding="utf-8"))  # noqa: SIM115
            output_stream = stack.enter_context(
                open("/dev/tty", "w", encoding="utf-8")  # noqa: SIM115
            )
    except OSError as exc:
        raise PlanSyntaxError("apply/revert requires an interactive controlling terminal") from exc
    return input_stream, output_stream


def require_controlling_terminal() -> None:
    """Require a human-owned terminal without asking an approval question."""

    if sys.stdin.isatty():
        return
    with ExitStack() as stack:
        _open_controlling_terminal(stack)


def _confirm(
    verb: str,
    operation_count: int,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> bool:
    """Require an explicit yes or no decision from the controlling terminal."""

    with ExitStack() as stack:
        if input_stream is None:
            if sys.stdin.isatty():
                input_stream = sys.stdin
                output_stream = output_stream or sys.stderr
            else:
                input_stream, terminal_output = _open_controlling_terminal(stack)
                output_stream = output_stream or terminal_output
        if output_stream is None:
            output_stream = sys.stderr
        prompt = f"{verb} these {operation_count} operations? [y = {verb.lower()}, n = cancel] "
        while True:
            output_stream.write(prompt)
            output_stream.flush()
            answer = input_stream.readline()
            if answer == "":
                output_stream.write("\nNo response received; cancelling safely.\n")
                output_stream.flush()
                return False
            normalized = answer.strip().lower()
            if normalized in {"y", "yes"}:
                return True
            if normalized in {"n", "no"}:
                return False
            output_stream.write("Please answer y or n; empty input is not a decision.\n")


def confirm_apply(
    operation_count: int,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> bool:
    """Ask once for apply approval."""

    return _confirm(
        "Apply",
        operation_count,
        input_stream=input_stream,
        output_stream=output_stream,
    )


def confirm_revert(
    operation_count: int,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> bool:
    """Ask once for revert approval."""

    return _confirm(
        "Revert",
        operation_count,
        input_stream=input_stream,
        output_stream=output_stream,
    )


def confirm_unattended_enable(
    account_email: str,
    max_impact: int,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> bool:
    """Ask a human to pin bounded unattended apply to one verified account."""

    with ExitStack() as stack:
        if input_stream is None:
            if sys.stdin.isatty():
                input_stream = sys.stdin
                output_stream = output_stream or sys.stderr
            else:
                input_stream, terminal_output = _open_controlling_terminal(stack)
                output_stream = output_stream or terminal_output
        if output_stream is None:
            output_stream = sys.stderr
        prompt = (
            f"Enable unattended apply for {account_email} with maximum impact "
            f"{max_impact}? [y = enable, n = cancel] "
        )
        while True:
            output_stream.write(prompt)
            output_stream.flush()
            answer = input_stream.readline()
            if answer == "":
                output_stream.write("\nNo response received; cancelling safely.\n")
                output_stream.flush()
                return False
            normalized = answer.strip().lower()
            if normalized in {"y", "yes"}:
                return True
            if normalized in {"n", "no"}:
                return False
            output_stream.write("Please answer y or n; empty input is not a decision.\n")
