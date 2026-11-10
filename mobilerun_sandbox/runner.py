"""The `run` command: validate input, call the entry point, write the result envelope.

Exit codes: 0 success; 1 the run failed (an error envelope was written: a `ScriptError`,
`script_error`, `output_invalid`, `result_too_large` or `state_too_large`); 2 `input_invalid`;
3 no valid entry point (no result file); 64 runner fault (bad flags or an unreadable or
unwritable file, no result file).
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
import typing
from dataclasses import dataclass
from functools import cache
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
from .context import RESULT_MAX_BYTES, STATE_MAX_BYTES, Context, RunInfo, State
from .entry import EntryPoint
from .errors import ScriptError
from .loader import ScriptImportError, load_script

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT_INVALID = 2
EXIT_NO_ENTRY_POINT = 3
EXIT_RUNNER_FAULT = 64

ERROR_MESSAGE_MAX_CHARS = 500
MAX_ERROR_DETAILS = 50


class RunnerFault(Exception):
    """The runner itself was misused (unreadable state file, ...)."""


@dataclass
class RunOptions:
    path: str
    input_file: str
    result_file: str
    run_info: RunInfo
    state_file: str | None = None


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
        "error": {"code": code, "message": message, "retryable": retryable, **extra},
    }


def _failure_from_exception(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ScriptError):
        return _failure(exc.code, exc.message, exc.retryable)
    traceback.print_exception(exc, file=sys.stderr)
    text = str(exc)[:ERROR_MESSAGE_MAX_CHARS]
    return _failure("script_error", f"{type(exc).__name__}: {text}")


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
    return State(configured=True, previous=parsed.data, previous_version=parsed.version)


def _read_input(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise RunnerFault(f"cannot read input file: {exc}") from exc


@cache
def _declared_names(model: type[BaseModel]) -> frozenset[str]:
    """Field names and aliases of `model` and every model nested in its annotations."""
    names: set[str] = set()
    seen: set[type[BaseModel]] = set()

    def visit(tp: Any) -> None:
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            if tp in seen:
                return
            seen.add(tp)
            for name, field in tp.model_fields.items():
                names.add(name)
                for alias in (field.alias, field.validation_alias, field.serialization_alias):
                    if isinstance(alias, str):
                        names.add(alias)
                visit(field.annotation)
            return
        for arg in typing.get_args(tp):
            visit(arg)

    visit(model)
    return frozenset(names)


def _validation_details(exc: ValidationError, model: type[BaseModel]) -> list[dict[str, str]]:
    """Declared field names, list indexes and error types only; never input values or keys."""
    declared = _declared_names(model)

    def part(component: Any) -> str:
        if isinstance(component, int) or component in declared:
            return str(component)
        return "*"

    return [
        {"path": ".".join(part(c) for c in err["loc"]), "message": err["type"]}
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ][:MAX_ERROR_DETAILS]


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
        if isinstance(value, BaseModel):
            # Re-validate through plain data: instances may come from model_construct or have
            # been changed after construction.
            value = value.model_dump(by_alias=True, warnings=False)
        validated = model.model_validate(value)
    except ValidationError as exc:
        return None, _failure(
            "output_invalid",
            f"the return value does not match {model.__name__}",
            details=_validation_details(exc, model),
        )
    # JSON text first: pydantic writes NaN and infinity as null.
    return json.loads(validated.model_dump_json()), None


def _call(entry: EntryPoint, model_input: BaseModel, ctx: Context) -> Any:
    args = (model_input, ctx) if entry.takes_ctx else (model_input,)
    result = entry.func(*args)
    if entry.is_async:
        result = asyncio.run(result)
    return result


def _execute(entry: EntryPoint, opts: RunOptions) -> tuple[dict[str, Any], int]:
    state = _read_state(opts.state_file)
    raw_input = _read_input(opts.input_file)
    ctx = Context(run=opts.run_info, state=state)
    try:
        try:
            model_input = entry.input_model.model_validate_json(raw_input)
        except ValidationError as exc:
            return _failure(
                "input_invalid",
                f"input does not match the input schema ({exc.error_count()} error(s))",
                details=_validation_details(exc, entry.input_model),
            ), EXIT_INPUT_INVALID
        value = _call(entry, model_input, ctx)
        result, failure = _serialise_result(entry, value)
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
        envelope["partialReason"] = ctx.partial_reason
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


def run(opts: RunOptions) -> int:
    """Run the script; returns the process exit code."""
    try:
        entry = load_script(opts.path)
    except OSError as exc:
        raise RunnerFault(f"cannot read script: {exc}") from exc
    except ScriptImportError as exc:
        _write_envelope(opts.result_file, _failure_from_exception(exc.original))
        return EXIT_FAILED
    envelope, code = _execute(entry, opts)
    _write_envelope(opts.result_file, envelope)
    return code
