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
        raise PlanSyntaxError(
            "apply/revert requires an interactive controlling terminal; no --yes mode exists"
        ) from exc
    return input_stream, output_stream


def confirm_apply(
    operation_count: int,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> bool:
    """Ask once for plan-level approval; default to no for every other response."""

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
        output_stream.write(f"Apply these {operation_count} operations? [y/N] ")
        output_stream.flush()
        answer = input_stream.readline()
        return answer.strip().lower() in {"y", "yes"}
