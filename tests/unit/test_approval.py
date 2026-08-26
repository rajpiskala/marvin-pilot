from __future__ import annotations

from io import StringIO

import pytest

import marvin_pilot.approval as approval
from marvin_pilot.approval import (
    confirm_apply,
    confirm_revert,
    require_controlling_terminal,
)
from marvin_pilot.errors import PlanSyntaxError


@pytest.mark.parametrize("answer", ["y\n", "Y\n", "yes\n", " YES \n"])
def test_confirmation_accepts_only_explicit_yes(answer: str) -> None:
    output = StringIO()
    assert confirm_apply(12, input_stream=StringIO(answer), output_stream=output)
    assert output.getvalue() == "Apply these 12 operations? [y = apply, n = cancel] "


@pytest.mark.parametrize("answer", ["n\n", "no\n", " N \n"])
def test_confirmation_accepts_only_explicit_no(answer: str) -> None:
    output = StringIO()
    assert not confirm_apply(1, input_stream=StringIO(answer), output_stream=output)
    assert output.getvalue() == "Apply these 1 operations? [y = apply, n = cancel] "


@pytest.mark.parametrize("first_answer", ["\n", "anything\n", "yess\n"])
def test_confirmation_reprompts_on_empty_or_unrecognized_input(first_answer: str) -> None:
    output = StringIO()
    answers = StringIO(first_answer + "yes\n")

    assert confirm_apply(1, input_stream=answers, output_stream=output)

    assert output.getvalue().count("Apply these 1 operations?") == 2
    assert "Please answer y or n; empty input is not a decision." in output.getvalue()


def test_confirmation_cancels_safely_on_end_of_input() -> None:
    output = StringIO()

    assert not confirm_apply(1, input_stream=StringIO(""), output_stream=output)

    assert "No response received; cancelling safely." in output.getvalue()


def test_missing_controlling_terminal_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    class NonTty:
        def isatty(self) -> bool:
            return False

    def fail(_stack):
        raise PlanSyntaxError("no terminal")

    monkeypatch.setattr(approval.sys, "stdin", NonTty())
    monkeypatch.setattr(approval, "_open_controlling_terminal", fail)
    with pytest.raises(PlanSyntaxError, match="no terminal"):
        confirm_apply(2)


def test_revert_uses_an_explicit_revert_prompt() -> None:
    output = StringIO()
    assert confirm_revert(2, input_stream=StringIO("yes\n"), output_stream=output)
    assert output.getvalue() == "Revert these 2 operations? [y = revert, n = cancel] "


def test_terminal_requirement_accepts_standard_input_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(approval.sys, "stdin", Tty())
    monkeypatch.setattr(
        approval,
        "_open_controlling_terminal",
        lambda _stack: pytest.fail("a TTY stdin should be sufficient"),
    )
    require_controlling_terminal()


def test_terminal_requirement_checks_platform_terminal_for_redirected_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonTty:
        def isatty(self) -> bool:
            return False

    checked: list[bool] = []

    def open_terminal(_stack):
        checked.append(True)
        return StringIO(), StringIO()

    monkeypatch.setattr(approval.sys, "stdin", NonTty())
    monkeypatch.setattr(approval, "_open_controlling_terminal", open_terminal)
    require_controlling_terminal()
    assert checked == [True]
