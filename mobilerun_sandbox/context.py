"""`Context`: what the platform provides to the entry point."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from .errors import NotAvailableError

RESULT_MAX_BYTES = 524288
STATE_MAX_BYTES = 524288
PARTIAL_REASON_MAX_CHARS = 500
STATUS_FILE_ENV = "MOBILERUN_SANDBOX_STATUS_FILE"


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    attempt: int
    deadline: datetime | None
    mode: Literal["live", "dry_run", "verification"]
    device_id: str | None


def _one_line(msg: object) -> str:
    return str(msg).replace("\r", "\\r").replace("\n", "\\n")


class Log:
    """Log lines go to stderr; `status` publishes the latest status through a file."""

    def _write(self, level: str, msg: object) -> None:
        sys.stderr.write(f"[{level}] {_one_line(msg)}\n")
        sys.stderr.flush()

    def info(self, msg: object) -> None:
        self._write("INFO", msg)

    def warning(self, msg: object) -> None:
        self._write("WARNING", msg)

    def error(self, msg: object) -> None:
        self._write("ERROR", msg)

    def status(self, msg: object) -> None:
        path = os.environ.get(STATUS_FILE_ENV)
        if not path:
            return
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(_one_line(msg) + "\n")
            os.replace(tmp, path)
        except OSError:
            pass  # status is best effort and never fails the script


@dataclass(frozen=True)
class StateSnapshot:
    """A snapshot recorded by `ctx.state.save`; `data` is JSON-compatible."""

    schema: int
    data: Any


class State:
    """Data carried from the last successful run to the next."""

    def __init__(
        self,
        configured: bool = False,
        previous: Any = None,
        previous_version: int | None = None,
    ) -> None:
        self._configured = configured
        self._previous = previous
        self._previous_version = previous_version
        self._saved: StateSnapshot | None = None

    @property
    def previous(self) -> Any:
        """Saved data of the last successful run, or `None`."""
        return self._previous

    @property
    def previous_version(self) -> int | None:
        return self._previous_version

    def save(self, data: Any, schema: int = 1) -> None:
        """Record the snapshot the platform stores after a successful run (last call wins)."""
        if not self._configured:
            raise NotAvailableError("state.save")
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise TypeError("schema must be an int")
        if isinstance(data, BaseModel):
            payload = data.model_dump_json()
        else:
            payload = json.dumps(data, allow_nan=False)
        # Copy now so later changes to `data` do not alter the snapshot.
        self._saved = StateSnapshot(schema=schema, data=json.loads(payload))

    @property
    def saved(self) -> StateSnapshot | None:
        return self._saved


class Context:
    """Passed to the entry point as `ctx`."""

    def __init__(
        self,
        run: RunInfo,
        log: Log | None = None,
        state: State | None = None,
    ) -> None:
        self.run = run
        self.log = log if log is not None else Log()
        self.state = state if state is not None else State()
        self._partial_reason: str | None = None

    def partial(self, reason: str) -> None:
        """Mark the run as partial; the entry point still returns its result."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        self._partial_reason = reason[:PARTIAL_REASON_MAX_CHARS]

    @property
    def partial_reason(self) -> str | None:
        return self._partial_reason

    def device(self) -> Any:
        raise NotAvailableError("device")

    @property
    def credentials(self) -> Any:
        raise NotAvailableError("credentials")

    @property
    def files(self) -> Any:
        raise NotAvailableError("files")

    @property
    def llm(self) -> Any:
        raise NotAvailableError("llm")
