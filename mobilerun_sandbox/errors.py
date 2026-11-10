"""Exceptions of the script interface."""

from __future__ import annotations


class ScriptError(Exception):
    """Raised by a script for a typed failure; the run ends with this code and message."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class EntryPointError(Exception):
    """The script has no usable entry point (missing, duplicate or wrong signature)."""


class NotAvailableError(Exception):
    """A `Context` member the platform does not provide in this SDK version."""

    def __init__(self, member: str) -> None:
        super().__init__(f"ctx.{member} is not available in this SDK version")
        self.member = member
