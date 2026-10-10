"""Domain exceptions shared across modules."""

from __future__ import annotations


class HRMError(Exception):
    """Base class for expected, handled errors."""


class NotFoundError(HRMError):
    pass


class ForbiddenError(HRMError):
    pass


class ConflictError(HRMError):
    pass


class ValidationFailed(HRMError):
    def __init__(self, message: str, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = issues or [message]


class LimitExceeded(HRMError):
    """A job exceeded a configured budget (tokens, tool calls, wall clock)."""
