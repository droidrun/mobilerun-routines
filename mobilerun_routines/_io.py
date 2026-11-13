"""File and JSON helpers shared by the runner and the context."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


def dumps_json(value: Any) -> str:
    """Compact JSON text; raises `ValueError` for NaN and infinity."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    """Write `text` to `path` through an exclusively created temp file in the same directory."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
