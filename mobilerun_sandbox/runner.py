"""The `run` command: validate input, call the entry point, write the result envelope."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .context import RESULT_MAX_BYTES, STATE_MAX_BYTES, Context, RunInfo, State
from .entry import EntryPoint
from .errors import ScriptError
from .loader import ScriptImportError, load_script

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INPUT_INVALID = 2
EXIT_NO_ENTRY_POINT = 3
EXIT_RUNNER_FAULT = 64  # bad flags or unreadable/unwritable files; never a result file

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


def _failure(code: str, message: str, retryable: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable, **extra},
    }


def _json_size(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(text.encode("utf-8"))


def _write_envelope(path: str, envelope: dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, path)
    except OSError as exc:
        raise RunnerFault(f"cannot write result file: {exc}") from exc


def _read_state(path: str | None) -> State:
    if path is None:
        return State(configured=False)
    try:
        raw = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise RunnerFault(f"cannot read state file: {exc}") from exc
    if raw is None:
        return State(configured=True)
    ok = (
        isinstance(raw, dict)
        and isinstance(raw.get("version"), int)
        and not isinstance(raw.get("version"), bool)
        and isinstance(raw.get("meta"), dict)
        and isinstance(raw["meta"].get("schema"), int)
        and "data" in raw
    )
    if not ok:
        raise RunnerFault("state file must be null or {version, meta: {schema}, data}")
    return State(configured=True, previous=raw["data"], previous_version=raw["version"])


def _validation_details(exc: ValidationError) -> list[dict[str, str]]:
    # Field paths and error types only; never input values.
    return [
        {"path": ".".join(str(part) for part in err["loc"]), "message": err["type"]}
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
        if isinstance(value, BaseModel) and not isinstance(value, model):
            value = value.model_dump()
        if not isinstance(value, model):
            value = model.model_validate(value)
        return json.loads(value.model_dump_json()), None
    except ValidationError as exc:
        return None, _failure(
            "output_invalid",
            f"the return value does not match {model.__name__}",
            details=_validation_details(exc),
        )


def _call(entry: EntryPoint, model_input: BaseModel, ctx: Context) -> Any:
    args = (model_input, ctx) if entry.takes_ctx else (model_input,)
    result = entry.func(*args)
    if entry.is_async:
        result = asyncio.run(result)
    return result


def _execute(entry: EntryPoint, opts: RunOptions) -> tuple[dict[str, Any], int]:
    state = _read_state(opts.state_file)
    try:
        model_input = entry.input_model.model_validate_json(Path(opts.input_file).read_bytes())
    except OSError as exc:
        raise RunnerFault(f"cannot read input file: {exc}") from exc
    except ValidationError as exc:
        return _failure(
            "input_invalid",
            f"input does not match the input schema ({exc.error_count()} error(s))",
            details=_validation_details(exc),
        ), EXIT_INPUT_INVALID

    ctx = Context(run=opts.run_info, state=state)
    try:
        value = _call(entry, model_input, ctx)
    except ScriptError as exc:
        return _failure(exc.code, exc.message, exc.retryable), EXIT_FAILED
    except (Exception, SystemExit) as exc:
        return _unexpected(exc), EXIT_FAILED

    result, failure = _serialise_result(entry, value)
    if failure is not None:
        return failure, EXIT_FAILED
    size = _json_size(result)
    if size > RESULT_MAX_BYTES:
        return _failure(
            "result_too_large", f"result is {size} bytes; the limit is {RESULT_MAX_BYTES}"
        ), EXIT_FAILED

    envelope: dict[str, Any] = {"ok": True, "outcome": "succeeded"}
    if ctx.partial_reason is not None:
        envelope["outcome"] = "partial"
        envelope["partialReason"] = ctx.partial_reason
    envelope["result"] = result
    saved = state.saved
    if saved is not None:
        state_size = _json_size(saved.data)
        if state_size > STATE_MAX_BYTES:
            return _failure(
                "state_too_large",
                f"state is {state_size} bytes; the limit is {STATE_MAX_BYTES}",
            ), EXIT_FAILED
        envelope["state"] = {"meta": {"schema": saved.schema}, "data": saved.data}
    return envelope, EXIT_OK


def _unexpected(exc: BaseException) -> dict[str, Any]:
    traceback.print_exception(exc, file=sys.stderr)
    text = str(exc)[:ERROR_MESSAGE_MAX_CHARS]
    return _failure("script_error", f"{type(exc).__name__}: {text}")


def run(opts: RunOptions) -> int:
    """Run the script; returns the process exit code."""
    try:
        entry = load_script(opts.path)
    except ScriptImportError as exc:
        if isinstance(exc.original, OSError):
            raise RunnerFault(f"cannot read script: {exc.original}") from exc
        envelope = _unexpected(exc.original)
        _write_envelope(opts.result_file, envelope)
        return EXIT_FAILED
    envelope, code = _execute(entry, opts)
    _write_envelope(opts.result_file, envelope)
    return code
