"""Stable error types and process exit codes."""

from __future__ import annotations

from typing import Any


class MarvinPilotError(Exception):
    """Base class for failures that can be shown without a traceback."""

    exit_code = 1

    def __init__(self, message: str, *, http_status_code: int | None = None) -> None:
        super().__init__(message)
        self.http_status_code = http_status_code


class PlanSyntaxError(MarvinPilotError):
    """The input is not valid strict JSON or does not match the plan schema."""

    exit_code = 2


class PlanSemanticError(MarvinPilotError):
    """The plan is structurally valid but internally inconsistent."""

    exit_code = 3


class CredentialError(MarvinPilotError):
    """A credential could not be loaded or authenticated."""

    exit_code = 4


class LivePreconditionError(MarvinPilotError):
    """Live Marvin state does not match the reviewed plan."""

    exit_code = 5

    def __init__(
        self,
        message: str,
        *,
        check: str = "live-precondition",
        expected: Any = None,
        found: Any = None,
    ) -> None:
        super().__init__(message)
        self.check = check
        self.expected = expected
        self.found = found


class UserDeclinedError(MarvinPilotError):
    """The human declined the interactive approval prompt."""

    exit_code = 6


class PartialMutationError(MarvinPilotError):
    """Some operations may have been applied; a receipt is available."""

    exit_code = 7


class RemoteError(MarvinPilotError):
    """The Marvin API failed before a mutation or during verification."""

    exit_code = 8


class AmbiguousMutationError(RemoteError):
    """A mutating request has an uncertain remote outcome that must be reconciled."""


class AmbiguousServerResponseError(AmbiguousMutationError):
    """A mutating request returned 5xx and its remote outcome must be reconciled."""


class HistoryError(MarvinPilotError):
    """Audit history could not be written safely."""

    exit_code = 9
