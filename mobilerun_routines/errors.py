"""Exceptions and error constants of the script interface."""

from __future__ import annotations

import re
from typing import Any

# Codes the runner itself produces. A `ScriptError` cannot use them: the platform tells runner
# failures from script failures by code, and by exit code for `input_invalid`.
RESERVED_ERROR_CODES = (
    "script_error",
    "input_invalid",
    "output_invalid",
    "result_too_large",
    "state_too_large",
    "dataset_too_large",
    "cancelled",
)
# Codes of the issues `inspect` reports (documented in the README).
INSPECT_ERROR_CODES = (
    "syntax_error",
    "import_wrong_module",
    "import_missing",
    "no_entry_point",
    "entry_point_invalid",
    "import_failed",
    "schema_unsupported",
)
ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Cap for `error.message` of every failure and for the reason of a partial result.
ERROR_MESSAGE_MAX_CHARS = 500


class ScriptError(Exception):
    """Raised by a script for a typed failure; the run ends with this code and message.

    The runner checks `code` when it builds the envelope: a code that does not match
    `^[a-z][a-z0-9_]{0,63}$` or is in `RESERVED_ERROR_CODES` is reported as `script_error`.
    """

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class EntryPointError(Exception):
    """The script has no usable entry point (missing, duplicate or wrong signature)."""


class NoEntryPointError(EntryPointError):
    """The script defines no function marked with `@script`."""


class NotAvailableError(Exception):
    """A `Context` member the platform does not provide in this run."""

    def __init__(self, member: str, reason: str | None = None) -> None:
        detail = reason or "is not available in this SDK version"
        super().__init__(f"ctx.{member} {detail}")
        self.member = member


class DatasetTooLargeError(Exception):
    """`ctx.dataset.push` would take the dataset file over `DATASET_MAX_BYTES`."""


class DatasetItemInvalidError(Exception):
    """An item given to `ctx.dataset.push` does not match the dataset's item model.

    `details` lists field paths and error types only, never input values.
    """

    def __init__(self, message: str, details: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.details: list[dict[str, Any]] = details
