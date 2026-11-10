"""The `run` command: validate input, call the entry point, write the result envelope.

Exit codes: 0 success; 1 the run failed (an error envelope was written: a `ScriptError`, or one
of the reserved codes `script_error`, `output_invalid`, `result_too_large`, `state_too_large`,
`dataset_too_large`, `cancelled`); 2 `input_invalid`; 3 no valid entry point (no result file);
64 runner fault (bad flags or an unreadable or unwritable file, no result file).
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    TypeAdapter,
    ValidationError,
)

from ._io import atomic_write_text, dumps_json
from ._validate import revalidate, validation_details
from .context import RESULT_MAX_BYTES, STATE_MAX_BYTES, Context, RunInfo, State
from .dataset import DatasetFile, DatasetSink
from .entry import EntryPoint
from .errors import (
    ERROR_CODE_PATTERN,
    ERROR_MESSAGE_MAX_CHARS,
    RESERVED_ERROR_CODES,
    DatasetItemInvalidError,
    DatasetTooLargeError,
    ScriptError,
)
from .loader import ScriptImportError, load_script

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT_INVALID = 2
EXIT_NO_ENTRY_POINT = 3
EXIT_RUNNER_FAULT = 64

CANCELLED_MESSAGE = "terminated by signal"


class RunnerFault(Exception):
    """The runner itself was misused (unreadable state file, ...)."""


class _Cancelled(BaseException):
    """Raised in the main thread by the SIGTERM handler; not an `Exception`, so script code
    that catches `Exception` does not swallow it."""


class _Cancellation:
    """Turns the first SIGTERM into `_Cancelled` while armed, and restores the old handler."""

    def __init__(self) -> None:
        self.armed = True
        self._installed = False
        self._previous: Any = None

    def _handle(self, signum: int, frame: Any) -> None:
        if self.armed:
            self.armed = False
            raise _Cancelled

    def install(self) -> None:
        try:
            self._previous = signal.signal(signal.SIGTERM, self._handle)
        except ValueError:  # not the main thread: signals cannot be handled here
            return
        self._installed = True

    def disarm(self) -> None:
        self.armed = False

    def restore(self) -> None:
        if self._installed and self._previous is not None:
            signal.signal(signal.SIGTERM, self._previous)


@dataclass
class RunOptions:
    path: str
    input_file: str
    result_file: str
    run_info: RunInfo
    state_file: str | None = None
    dataset_file: str | None = None


class _StateMeta(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_: StrictInt = Field(validation_alias=AliasChoices("schema", "schema_"))


class _StateFile(BaseModel):
    version: StrictInt
    meta: _StateMeta
    data: Any


_STATE_FILE = TypeAdapter(_StateFile | None)


def _failure(code: str, message: str, retryable: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message[:ERROR_MESSAGE_MAX_CHARS],
            "retryable": retryable,
            **extra,
        },
    }


def _script_error_code(code: Any) -> str:
    """The script's code if it is well formed and not reserved, else `script_error`."""
    if (
        isinstance(code, str)
        and ERROR_CODE_PATTERN.fullmatch(code)
        and code not in RESERVED_ERROR_CODES
    ):
        return code
    return "script_error"


def _failure_from_exception(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ScriptError):
        return _failure(_script_error_code(exc.code), str(exc.message), bool(exc.retryable))
    if isinstance(exc, DatasetTooLargeError):
        return _failure("dataset_too_large", str(exc))
    if isinstance(exc, DatasetItemInvalidError):
        return _failure("output_invalid", str(exc), details=exc.details)
    traceback.print_exception(exc, file=sys.stderr)
    return _failure("script_error", f"{type(exc).__name__}: {exc}")


def _size_failure(value: Any, limit: int, code: str, what: str) -> dict[str, Any] | None:
    size = len(dumps_json(value).encode("utf-8"))
    if size <= limit:
        return None
    return _failure(code, f"{what} is {size} bytes; the limit is {limit}")


def _read_state(path: str | None) -> State:
    if path is None:
        return State(configured=False)
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise RunnerFault(f"cannot read state file: {exc}") from exc
    try:
        parsed = _STATE_FILE.validate_json(raw)
    except ValidationError as exc:
        raise RunnerFault("state file must be null or {version, meta: {schema}, data}") from exc
    if parsed is None:
        return State(configured=True)
    return State(configured=True, previous=parsed.data, previous_schema=parsed.meta.schema_)


def _read_input(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise RunnerFault(f"cannot read input file: {exc}") from exc


def _serialise_result(entry: EntryPoint, value: Any) -> tuple[Any, dict[str, Any] | None]:
    """Return `(result_json, failure)`; failure is set when the value breaks the contract."""
    model = entry.output_model
    if model is None:
        if value is not None:
            return None, _failure(
                "output_invalid",
                "the entry point returned a value but declares no output model",
            )
        return None, None
    if value is None:
        return None, None
    try:
        # Instances are validated again: they may come from model_construct or have been
        # changed after construction.
        validated = revalidate(model, value)
    except ValidationError as exc:
        return None, _failure(
            "output_invalid",
            f"the return value does not match {model.__name__}",
            details=validation_details(exc, model),
        )
    # JSON text first: pydantic writes NaN and infinity as null. Keys are the serialization
    # names, the ones `outputSchema` advertises.
    return json.loads(validated.model_dump_json(by_alias=True)), None


def _call(entry: EntryPoint, model_input: BaseModel, ctx: Context) -> Any:
    args = (model_input, ctx) if entry.takes_ctx else (model_input,)
    result = entry.func(*args)
    if entry.is_async:
        result = asyncio.run(result)
    return result


def _dataset_sink(entry: EntryPoint, dataset: DatasetFile | None) -> DatasetSink:
    if dataset is None:
        return DatasetSink()
    if entry.output_kind != "dataset":
        return DatasetSink(
            unavailable="is only available when the entry point returns Dataset[Item]"
        )
    return DatasetSink(dataset, entry.item_type)


def _execute(
    entry: EntryPoint, opts: RunOptions, dataset: DatasetFile | None
) -> tuple[dict[str, Any], int]:
    state = _read_state(opts.state_file)
    raw_input = _read_input(opts.input_file)
    ctx = Context(run=opts.run_info, state=state, dataset=_dataset_sink(entry, dataset))
    try:
        try:
            model_input = entry.input_model.model_validate_json(raw_input)
        except ValidationError as exc:
            return _failure(
                "input_invalid",
                f"input does not match the input schema ({exc.error_count()} error(s))",
                details=validation_details(exc, entry.input_model),
            ), EXIT_INPUT_INVALID
        value = _call(entry, model_input, ctx)
        result, failure = _serialise_result(entry, value)
        if failure is None and dataset is not None and result is not None:
            # With a dataset file the returned items follow the pushed ones; `result` stays empty.
            if entry.output_kind == "dataset":
                for item in result["items"]:
                    dataset.append(dumps_json(item))
                result = None
    except (Exception, SystemExit) as exc:
        # ScriptError stays typed; anything else raised by script code or its validators and
        # serializers is a script_error.
        return _failure_from_exception(exc), EXIT_FAILED
    if failure is None:
        failure = _size_failure(result, RESULT_MAX_BYTES, "result_too_large", "result")
    if failure is not None:
        return failure, EXIT_FAILED

    envelope: dict[str, Any] = {"ok": True, "outcome": "succeeded"}
    if ctx.partial_reason is not None:
        envelope["outcome"] = "partial"
        envelope["partialReason"] = ctx.partial_reason[:ERROR_MESSAGE_MAX_CHARS]
    envelope["result"] = result
    saved = state.saved
    if saved is not None:
        failure = _size_failure(saved.data, STATE_MAX_BYTES, "state_too_large", "state")
        if failure is not None:
            return failure, EXIT_FAILED
        envelope["state"] = {"meta": {"schema": saved.schema}, "data": saved.data}
    return envelope, EXIT_OK


def _write_envelope(path: str, envelope: dict[str, Any]) -> None:
    try:
        atomic_write_text(path, dumps_json(envelope))
    except OSError as exc:
        raise RunnerFault(f"cannot write result file: {exc}") from exc


def _open_dataset_file(path: str | None) -> DatasetFile | None:
    if path is None:
        return None
    try:
        return DatasetFile(path)
    except OSError as exc:
        raise RunnerFault(f"cannot write dataset file: {exc}") from exc


def _run_script(opts: RunOptions, dataset: DatasetFile | None) -> tuple[dict[str, Any], int]:
    try:
        entry = load_script(opts.path)
    except OSError as exc:
        raise RunnerFault(f"cannot read script: {exc}") from exc
    except ScriptImportError as exc:
        return _failure_from_exception(exc.original), EXIT_FAILED
    return _execute(entry, opts, dataset)


def run(opts: RunOptions) -> int:
    """Run the script; returns the process exit code.

    SIGTERM ends the script with a `cancelled` envelope (exit 1); pushed dataset lines stay.
    """
    cancellation = _Cancellation()
    cancellation.install()
    dataset = None
    try:
        dataset = _open_dataset_file(opts.dataset_file)
        try:
            envelope, code = _run_script(opts, dataset)
            cancellation.disarm()
        except _Cancelled:
            envelope, code = _failure("cancelled", CANCELLED_MESSAGE), EXIT_FAILED
        if dataset is not None:
            envelope["dataset"] = dataset.stats()
        _write_envelope(opts.result_file, envelope)
        return code
    finally:
        cancellation.restore()
        if dataset is not None:
            dataset.close()
