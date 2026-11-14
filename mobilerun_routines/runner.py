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
from dataclasses import dataclass, field
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
    """Turns SIGTERM into `_Cancelled` in the main thread and remembers that it arrived.

    A script may swallow `_Cancelled` (`finally: return`), so `requested` decides the outcome.
    """

    def __init__(self) -> None:
        self.armed = True
        self.requested = False
        self._previous: Any = None  # the handler to restore; `None` if none was installed

    def _handle(self, signum: int, frame: Any) -> None:
        self.requested = True
        if self.armed:
            self.armed = False
            raise _Cancelled

    def install(self) -> None:
        try:
            self._previous = signal.signal(signal.SIGTERM, self._handle)
        except ValueError:
            pass  # not the main thread: signals cannot be handled here

    def disarm(self) -> None:
        self.armed = False

    def restore(self) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGTERM, self._previous)


@dataclass
class RunOptions:
    path: str
    input_file: str
    result_file: str
    run_info: RunInfo
    state_file: str | None = None
    dataset_file: str | None = None
    # Set once the input validated, before the script runs (a script may change its input
    # model): the `input` of the envelope. `None` when absent, too big or not valid JSON.
    echoed_input: dict[str, Any] | None = field(default=None, init=False, repr=False)


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
            "message": str.__str__(message)[:ERROR_MESSAGE_MAX_CHARS],
            "retryable": retryable,
            **extra,
        },
    }


_UNFORMATTABLE = "error message could not be formatted"


def _script_error_code(code: Any) -> str:
    """The script's code if it is well formed and not reserved, else `script_error`."""
    if not isinstance(code, str):
        return "script_error"
    code = str.__str__(code)  # a plain str: a subclass could lie in `==`
    if ERROR_CODE_PATTERN.fullmatch(code) and code not in RESERVED_ERROR_CODES:
        return code
    return "script_error"


def _failure_from_exception(exc: BaseException) -> dict[str, Any]:
    """The envelope for `exc`; never raises, whatever the exception's own methods do."""
    try:
        if isinstance(exc, ScriptError):
            return _failure(_script_error_code(exc.code), str(exc.message), bool(exc.retryable))
        if isinstance(exc, DatasetTooLargeError):
            return _failure("dataset_too_large", str(exc))
        if isinstance(exc, DatasetItemInvalidError):
            return _failure("output_invalid", str(exc), details=exc.details)
        traceback.print_exception(exc, file=sys.stderr)
        return _failure("script_error", f"{type(exc).__name__}: {exc}")
    except Exception:
        return _failure("script_error", _UNFORMATTABLE)


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
        validated = revalidate(TypeAdapter(model), value)
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


def _spill_to_dataset(
    entry: EntryPoint, dataset: DatasetFile | None, result: Any
) -> Any:
    """With a dataset file the returned items follow the pushed ones and `result` is empty."""
    if dataset is None or result is None or entry.output_kind != "dataset":
        return result
    for item in result["items"]:
        dataset.append(dumps_json(item))
    return None


def _execute(
    entry: EntryPoint, opts: RunOptions, dataset: DatasetFile | None
) -> tuple[dict[str, Any], int]:
    state = _read_state(opts.state_file)
    raw_input = _read_input(opts.input_file)
    ctx = Context(run=opts.run_info, state=state, dataset=DatasetSink(dataset, entry.item_type))
    try:
        try:
            model_input = entry.input_model.model_validate_json(raw_input)
        except ValidationError as exc:
            return _failure(
                "input_invalid",
                f"input does not match the input schema ({exc.error_count()} error(s))",
                details=validation_details(exc, entry.input_model),
            ), EXIT_INPUT_INVALID
        opts.echoed_input = _echo_input(model_input)
        value = _call(entry, model_input, ctx)
        result, failure = _serialise_result(entry, value)
        if failure is None:
            result = _spill_to_dataset(entry, dataset, result)
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


# `input` is left out above this size (compact JSON), so it never crowds out the result.
INPUT_ECHO_MAX_BYTES = 65536


def _echo_input(model_input: BaseModel) -> dict[str, Any] | None:
    """The `input` of the envelope, or `None` when it is too big or not valid JSON.

    Never raises: the input is a courtesy and must not change how a run ends.
    """
    try:
        value = model_input.model_dump(mode="json", by_alias=True)
        if len(dumps_json(value).encode("utf-8")) > INPUT_ECHO_MAX_BYTES:
            return None
    except Exception:
        return None
    return value


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

    SIGTERM ends the run with a `cancelled` envelope (exit 1), whatever the script does after
    the signal; the dataset file keeps its complete lines.
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
            pass
        if cancellation.requested:
            envelope, code = _failure("cancelled", CANCELLED_MESSAGE), EXIT_FAILED
        if dataset is not None:
            dataset.finalise()
            envelope["dataset"] = dataset.stats()
        if opts.echoed_input is not None:
            envelope["input"] = opts.echoed_input
        _write_envelope(opts.result_file, envelope)
        return code
    finally:
        cancellation.restore()
        if dataset is not None:
            dataset.close()
