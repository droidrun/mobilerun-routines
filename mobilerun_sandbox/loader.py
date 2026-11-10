"""Load a script file and find its entry point."""

from __future__ import annotations

import sys
import types
from pathlib import Path

from .entry import _ENTRY_POINTS, EntryPoint
from .errors import EntryPointError

MODULE_NAME = "mobilerun_user_script"


class ScriptImportError(Exception):
    """The script file could not be imported (missing file, syntax error, exception at import)."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__(f"{type(exc).__name__}: {exc}")
        self.original = exc


def load_script(path: str | Path) -> EntryPoint:
    """Execute the script module and return its entry point.

    Raises `EntryPointError` for a missing or invalid entry point and `ScriptImportError` when
    importing the script itself fails.
    """
    _ENTRY_POINTS.pop(MODULE_NAME, None)
    sys.modules.pop(MODULE_NAME, None)
    try:
        source = Path(path).read_bytes()
        code = compile(source, str(path), "exec")
    except (OSError, SyntaxError, ValueError) as exc:
        raise ScriptImportError(exc) from exc
    module = types.ModuleType(MODULE_NAME)
    module.__file__ = str(path)
    sys.modules[MODULE_NAME] = module
    try:
        exec(code, module.__dict__)  # noqa: S102 - running the script is the point
    except EntryPointError:
        raise
    except Exception as exc:
        raise ScriptImportError(exc) from exc
    entry = _ENTRY_POINTS.get(MODULE_NAME)
    if entry is None:
        raise EntryPointError("no entry point: mark one function with @script")
    return entry
