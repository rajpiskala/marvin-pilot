"""Stable error types and process exit codes."""

from __future__ import annotations


class MarvinPilotError(Exception):
    """Base class for failures that can be shown without a traceback."""

    exit_code = 1


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
