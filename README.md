# mobilerun-routines

Python package for writing Mobilerun routine scripts. A script has one typed entry point that
receives a validated input model and a `Context` with what the platform provides, and returns
an output model. The package also ships the `inspect` and `run` commands the platform uses to
read a script's schema and to execute it.

Requires Python 3.11 to 3.13 and `pydantic>=2.11` (instances are re-validated by field name,
which needs `by_name` validation from pydantic 2.11).

## Install

```sh
pip install mobilerun-routines
```

Python 3.11, 3.12 or 3.13. For device control add the extra: `pip install "mobilerun-routines[device]"`.

## Sample script

```python
from datetime import date

from pydantic import BaseModel, Field
from mobilerun_routines import Context, Dataset, ScriptError, key_field, script


class Input(BaseModel):
    query: str
    since: date  # a `format: date` input; the script receives a `date`
    max_pages: int = 3


class Product(BaseModel):
    sku: str = key_field()  # the field that identifies an item between runs
    name: str
    unit_price: float = Field(serialization_alias="unitPrice")


@script
async def main(input: Input, ctx: Context) -> Dataset[Product] | None:
    if input.max_pages < 1:
        raise ScriptError("bad_pages", "max_pages must be at least 1")
    ctx.log.info(f"searching {input.query}")
    for page in range(1, input.max_pages + 1):
        try:
            rows = await fetch_page(input.query, page)  # your code
        except TimeoutError:
            ctx.partial(f"page {page} timed out")
            break
        for row in rows:
            ctx.dataset.push(Product(sku=row["sku"], name=row["name"], unit_price=row["price"]))
    # nothing to return: every item was pushed
```

`def` works the same way as `async def`, and `ctx` may be left out of the signature. Return a
`Dataset[Item]` for a list the platform can diff, call `ctx.partial("reason")` to mark a
partial result, and use `ctx.state` to carry data to the next run.

## Commands

```sh
# write a starter script (fails if the file exists; the directory must exist)
python -m mobilerun_routines new hello.py

# print the entry point and the input/output JSON Schema (exit 3 if there is no valid entry point)
python -m mobilerun_routines inspect script.py

# validate input, call the entry point, write one result envelope
python -m mobilerun_routines run script.py --input-file in.json --result-file out.json \
    [--state-file state.json] [--dataset-file dataset.jsonl]
```

### `new`

`new PATH` writes a short working script (an `Input` and an `Output` model and a `@script` entry
point) to `PATH` and prints the path. It never overwrites: if `PATH` exists, or its directory is
missing, it prints one line on stderr and exits 64. The starter passes `inspect` as written.

### `inspect`

On success it prints one JSON object on stdout and exits 0:
`{"ok": true, "entrypoint", "async", "inputSchema", "outputSchema", "outputKind", "datasetKey",
"sdkVersion"}`. `outputKind` is `value`, `dataset` or `none`. `datasetKey` is the serialization
name of the item field marked with `key_field()` for a `Dataset[Item]` output, else `null`.
The script runs when it is imported; what it prints goes to stderr, so stdout holds exactly the
JSON document (output written straight to file descriptor 1 is not intercepted).

On failure it exits 3 and prints `{"ok": false, "error": {"code", "message"}}` on stdout (the
one-line message goes to stderr as well). Codes: `no_entry_point`, `entry_point_invalid`,
`import_failed` (the script cannot be read or raises while it is imported) and
`schema_unsupported` (a model uses something outside the supported JSON Schema subset, or a
`Dataset` item model does not have exactly one key field that serialises to a string or an
integer).

The schemas are limited to a subset the platform can validate. `pattern`, `prefixItems`,
`discriminator` and recursive models are rejected, and so is any `format` outside this list:

`date-time`, `date`, `uuid`, `uri`, `email`

That covers `datetime`, `date`, `UUID`, `AnyUrl`/`HttpUrl` and, for `email`, a `str` field with
`Field(json_schema_extra={"format": "email"})`. `run` still validates the input with the
Pydantic model, so a `datetime` field receives a parsed value.

### `run`

Exit codes: `0` success; `1` the run failed (error envelope); `2` invalid input; `3` no valid
entry point (no result file); `64` runner fault such as bad flags or an unreadable file (no
result file). The envelope is
`{"ok": true, "outcome": "succeeded" | "partial", "result": ...}` (a partial one also has
`partialReason`; with `ctx.state.save` it has `state`) or
`{"ok": false, "error": {"code", "message", "retryable"}}`.

Result keys are the serialization names of the output model (`serialization_alias`, or `alias`),
exactly the property names `outputSchema` lists.

**Error codes.** A script raises `ScriptError(code, message, retryable=False)`. The code must
match `^[a-z][a-z0-9_]{0,63}$` and must not be one of the codes the runner uses itself;
otherwise the envelope code is `script_error` (message and `retryable` are kept). The check
happens when the envelope is built.

<!-- reserved-codes:start -->
Reserved codes: `script_error` (an unexpected exception), `input_invalid` (exit 2; field paths
and error types only), `output_invalid` (the return value or a pushed item does not match its
model), `result_too_large`, `state_too_large`, `dataset_too_large`, `cancelled` (SIGTERM).
<!-- reserved-codes:end -->

`script_error` messages contain `str(exc)` and may include input values. The platform scrubs
them; this package does not.

**Limits.**

- `error.message` and `partialReason` are cut to 500 characters (`ERROR_MESSAGE_MAX_CHARS`).
- `result`: 524288 bytes of compact JSON (`RESULT_MAX_BYTES`).
- `state`: 524288 bytes (`STATE_MAX_BYTES`).
- dataset file: 524288 bytes (`DATASET_MAX_BYTES`).

## Datasets

Return `Dataset[Item]` for a list the platform can diff between runs. Mark the field that
identifies an item with `key_field()` (a `Field(...)` that accepts the usual arguments); exactly
one field must carry it, and it must serialise to a string or an integer (`str`, `int`, `UUID`,
...; not `float`, `bool` or a list). The package does not check that keys are unique.
`Dataset` has only `items`: report an incomplete list with `ctx.partial(reason)`.

A long run should not lose what it found, so `run --dataset-file PATH` creates (and truncates)
a JSON Lines file and `ctx.dataset.push(item)` appends one validated item to it at once.
`push` is only available when the entry point returns `Dataset[Item]` (or `Dataset[Item] |
None`) and the platform passed `--dataset-file`; otherwise it raises `NotAvailableError`. An
item is an `Item` or data valid for `Item`; a bad one raises `DatasetItemInvalidError`
(`output_invalid`). A line that would take the file over 524288 bytes raises
`DatasetTooLargeError` (`dataset_too_large`, exit 1); the lines before stay, and a script may
catch the error and call `ctx.partial`.

With `--dataset-file`, items the entry point returns are appended after the pushed ones and
`result` is `null`; every envelope carries `"dataset": {"items": N, "bytes": B}` for the lines
this run wrote. Without it, `result` holds `{"items": [...]}` as before.

## State and cancellation

`ctx.state.previous` is the data of the last successful run and `ctx.state.previous_schema` the
`schema` it was saved with (`None` without a previous state), so a script can migrate a changed
shape: `ctx.state.save(data, schema=2)`.

On SIGTERM, `run` stops the entry point (sync or async) and the outcome is `cancelled`, even if
the script swallows the interruption or fails while cleaning up. It writes
`{"ok": false, "error": {"code": "cancelled", "message": "terminated by signal", "retryable":
false}}` (plus the `dataset` counts when a dataset file is in use) and exits 1. Lines already
pushed stay on disk; the file only ever holds complete lines, also after an interrupted write.
`push` is safe to call from several threads. The package has no timeout, deadline or retry logic; the platform owns those.

## Control the device

A run started with a device gives the script `ctx.device()`. It is synchronous and returns the
`mobilerun-core` device of the run, connected once and reused on every call:

```python
@script
def main(input: Input, ctx: Context) -> None:
    device = ctx.device()
    path = device.screenshot()  # path of a temporary image file
    ui = device.ui()  # UI tree; `ui.nodes` lists the elements
    device.tap(540, 1200)
    device.type("hello")
    device.start_app("com.android.settings")
```

The device is the unchanged `mobilerun_core.Device`; see that package for all its methods. It is
an optional dependency: install `mobilerun-routines[device]`. A run without a device, without
`mobilerun-core`, or without the platform's environment raises `NotAvailableError`; scripts
never build a client or read the environment. The platform handles the credentials.

`credentials`, `files` and `llm` on `Context` raise `NotAvailableError` in this version.
Develop with `uv sync && uv run ruff check && uv run pytest`.
