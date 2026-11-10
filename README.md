# mobilerun-sandbox

Python package for writing Mobilerun sandbox scripts. A script has one typed entry point that
receives a validated input model and a `Context` with what the platform provides, and returns
an output model. The package also ships the `inspect` and `run` commands the platform uses to
read a script's schema and to execute it.

## Sample script

```python
from pydantic import BaseModel
from mobilerun_sandbox import Context, ScriptError, script


class Input(BaseModel):
    name: str
    times: int = 1


class Output(BaseModel):
    greeting: str


@script
def main(input: Input, ctx: Context) -> Output:
    if input.times < 1:
        raise ScriptError("bad_times", "times must be at least 1")
    ctx.log.info(f"greeting {input.name}")
    return Output(greeting=" ".join([f"Hello {input.name}!"] * input.times))
```

`async def` works the same way, and `ctx` may be left out of the signature. Return a
`Dataset[Item]` for a list the platform can diff, call `ctx.partial("reason")` to mark a
partial result, and use `ctx.state` to carry data to the next run.

## Commands

```sh
# print the entry point and the input/output JSON Schema (exit 3 if there is no valid entry point)
python -m mobilerun_sandbox inspect script.py

# validate input, call the entry point, write one result envelope
python -m mobilerun_sandbox run script.py --input-file in.json --result-file out.json
```

Exit codes of `run`: `0` success, `1` the script failed, `2` invalid input, `3` no valid entry
point (no result file). The envelope is `{"ok": true, "outcome": "succeeded", "result": ...}`
or `{"ok": false, "error": {"code", "message", "retryable"}}`. Results and saved state are
limited to 512 KiB each.

## Development

```sh
uv sync && uv run ruff check && uv run pytest
```

`device()`, `credentials`, `files` and `llm` on `Context` raise `NotAvailableError` in this
version.
