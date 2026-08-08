from __future__ import annotations

from io import StringIO

import pytest

import marvin_pilot.approval as approval
from marvin_pilot.approval import confirm_apply
from marvin_pilot.errors import PlanSyntaxError


@pytest.mark.parametrize("answer", ["y\n", "Y\n", "yes\n", " YES \n"])
def test_confirmation_accepts_only_explicit_yes(answer: str) -> None:
    output = StringIO()
    assert confirm_apply(12, input_stream=StringIO(answer), output_stream=output)
    assert output.getvalue() == "Apply these 12 operations? [y/N] "


@pytest.mark.parametrize("answer", ["\n", "n\n", "no\n", "anything\n", "yess\n"])
def test_confirmation_defaults_to_no(answer: str) -> None:
    assert not confirm_apply(1, input_stream=StringIO(answer), output_stream=StringIO())


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
