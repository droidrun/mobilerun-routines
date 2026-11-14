"""Load a script file and find its entry point."""

from __future__ import annotations

import sys
import types
from pathlib import Path

from .entry import EntryPoint, take
from .errors import NO_ENTRY_POINT_MESSAGE, EntryPointError, NoEntryPointError

MODULE_NAME = "mobilerun_user_script"


class ScriptImportError(Exception):
    """Executing the script module failed (syntax error, any exception or exit at import)."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(f"{type(exc).__name__}: {exc}")
        self.original = exc


class ScriptSyntaxError(ScriptImportError):
    """The script does not compile (`original` is the `SyntaxError` or `ValueError`)."""


def load_script(path: str | Path) -> EntryPoint:
    """Execute the script module and return its entry point.

    Raises `OSError` when the file cannot be read, `ScriptImportError` when compiling or executing
    it fails (`ScriptSyntaxError` for the former) and `EntryPointError` for a missing or invalid
    entry point.
    """
    source = Path(path).read_bytes()
    take(MODULE_NAME)
    sys.modules.pop(MODULE_NAME, None)
    try:
        code = compile(source, str(path), "exec")
    except (SyntaxError, ValueError) as exc:
        raise ScriptSyntaxError(exc) from exc
    module = types.ModuleType(MODULE_NAME)
    module.__file__ = str(path)
    sys.modules[MODULE_NAME] = module
    try:
        exec(code, module.__dict__)  # noqa: S102 - running the script is the point
    except EntryPointError:
        raise
    except (Exception, SystemExit) as exc:
        raise ScriptImportError(exc) from exc
    entry = take(MODULE_NAME)
    if entry is None:
        raise NoEntryPointError(NO_ENTRY_POINT_MESSAGE)
    return entry
