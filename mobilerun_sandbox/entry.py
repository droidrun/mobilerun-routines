"""The `script` decorator and entry point signature checks."""

from __future__ import annotations

import inspect
import types
import typing
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from .context import Context
from .dataset import Dataset
from .errors import EntryPointError

OutputKind = Literal["value", "dataset", "none"]


@dataclass(frozen=True)
class EntryPoint:
    func: Any
    name: str
    is_async: bool
    takes_ctx: bool
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None

    @property
    def output_kind(self) -> OutputKind:
        if self.output_model is None:
            return "none"
        return "dataset" if issubclass(self.output_model, Dataset) else "value"


# Entry points by the `__module__` of the decorated function (one per module).
_ENTRY_POINTS: dict[str, EntryPoint] = {}


def take(module: str) -> EntryPoint | None:
    """Remove and return the entry point registered for `module`, if any."""
    return _ENTRY_POINTS.pop(module, None)


def reset() -> None:
    """Forget every registered entry point."""
    _ENTRY_POINTS.clear()


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _describe(tp: Any) -> str:
    return tp.__name__ if isinstance(tp, type) else repr(tp)


def _output_model(hints: dict[str, Any]) -> type[BaseModel] | None:
    if "return" not in hints:
        return None
    tp = hints["return"]
    if tp is type(None):
        return None
    if typing.get_origin(tp) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            tp = args[0]
    if not _is_model(tp):
        raise EntryPointError(
            f"return: annotation must be a pydantic BaseModel subclass, "
            f"'OutputModel | None' or None, got {_describe(tp)}"
        )
    return tp


def _build(func: Any) -> EntryPoint:
    if not inspect.isfunction(func):
        raise EntryPointError("@script must decorate a plain function")
    if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
        raise EntryPointError(f"{func.__name__}: entry point must not be a generator")

    params = list(inspect.signature(func).parameters.values())
    for p in params:
        if p.kind not in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            raise EntryPointError(
                f"parameter '{p.name}': only plain positional parameters (input, ctx) are allowed"
            )
    if not params:
        raise EntryPointError("missing parameter 'input': first parameter must be the input model")
    if len(params) > 2:
        raise EntryPointError(f"parameter '{params[2].name}': at most two parameters (input, ctx)")

    try:
        hints = typing.get_type_hints(func)
    except Exception as exc:
        raise EntryPointError(f"cannot resolve annotations: {type(exc).__name__}: {exc}") from exc

    first = params[0]
    if first.name not in hints:
        raise EntryPointError(
            f"parameter '{first.name}': missing annotation (a pydantic BaseModel)"
        )
    input_model = hints[first.name]
    if not _is_model(input_model):
        raise EntryPointError(
            f"parameter '{first.name}': annotation must be a pydantic BaseModel subclass, "
            f"got {_describe(input_model)}"
        )

    takes_ctx = len(params) == 2
    if takes_ctx:
        ctx_name = params[1].name
        if ctx_name not in hints:
            raise EntryPointError(f"parameter '{ctx_name}': missing annotation (Context)")
        if hints[ctx_name] is not Context:
            raise EntryPointError(
                f"parameter '{ctx_name}': annotation must be Context, "
                f"got {_describe(hints[ctx_name])}"
            )

    output_model = _output_model(hints)
    return EntryPoint(
        func=func,
        name=func.__name__,
        is_async=inspect.iscoroutinefunction(func),
        takes_ctx=takes_ctx,
        input_model=input_model,
        output_model=output_model,
    )


def script(func: Any = None) -> Any:
    """Mark the entry point: `def|async def main(input: In, ctx: Context) -> Out | None`."""
    if func is None or not callable(func):
        raise EntryPointError("@script takes no arguments; use it as a bare decorator")
    entry = _build(func)
    module = func.__module__
    if module in _ENTRY_POINTS:
        raise EntryPointError(
            f"duplicate entry point '{entry.name}': '{_ENTRY_POINTS[module].name}' is already "
            "marked with @script (one per module)"
        )
    _ENTRY_POINTS[module] = entry
    return func
