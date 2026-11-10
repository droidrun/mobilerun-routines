"""Build small sample scripts for the tests."""

from __future__ import annotations

import textwrap

PRELUDE = """
from datetime import date, datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_serializer, field_validator
from mobilerun_sandbox import (
    RESULT_MAX_BYTES, STATE_MAX_BYTES, Context, Dataset, NotAvailableError, ScriptError, script,
)
"""

MODELS = """
class In(BaseModel):
    name: str = "x"
    count: int = 1
    extra: int = 0


class Out(BaseModel):
    greeting: str
"""


def script_source(
    body: str = "pass",
    *,
    sig: str = "input: In, ctx: Context",
    ret: str | None = "Out",
    models: str = MODELS,
    before: str = "",
    decorator: str = "@script",
    is_async: bool = False,
) -> str:
    """A script with `body` as the entry point's body; `ret=None` omits the return annotation."""
    returns = f" -> {ret}" if ret is not None else ""
    prefix = "async def" if is_async else "def"
    lines = [
        PRELUDE,
        textwrap.dedent(models),
        textwrap.dedent(before),
        decorator,
        f"{prefix} main({sig}){returns}:",
        textwrap.indent(textwrap.dedent(body).strip("\n"), "    "),
        "",
    ]
    return "\n".join(lines)


GREETER = script_source(
    """
    print("hello from script")
    return Out(greeting=f"hi {input.name} x{input.count}")
    """
)
