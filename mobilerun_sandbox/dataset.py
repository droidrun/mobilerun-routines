"""`Dataset`, `ctx.dataset.push` and the JSON Lines file behind it."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ._validate import revalidate, validation_details
from .errors import DatasetItemInvalidError, DatasetTooLargeError, NotAvailableError

DATASET_MAX_BYTES = 524288
KEY_MARKER = "x-mobilerun-key"

ItemT = TypeVar("ItemT")


class Dataset(BaseModel, Generic[ItemT]):
    """Return `Dataset[Item]` to publish a list of items the platform can diff.

    An incomplete list is reported with `ctx.partial(reason)`, not with a field here.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[ItemT]


def key_field(**kwargs: Any) -> Any:
    """`Field(...)` that marks the field identifying a dataset item between runs.

    Exactly one field of an item model carries it, and it must serialise to a string or an
    integer. Other arguments go to `Field` unchanged; a `json_schema_extra` dict is kept next to
    the marker.
    """
    extra = kwargs.pop("json_schema_extra", None) or {}
    return Field(json_schema_extra={**extra, KEY_MARKER: True}, **kwargs)


class DatasetFile:
    """The JSON Lines file of one run: created empty at start, only complete lines survive.

    One lock covers the size check, the write and the accounting, so threads cannot exceed the
    limit together or interleave lines. `finalise` cuts the file back to the last complete line.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fh = open(self.path, "wb", buffering=0)  # noqa: SIM115 - closed in `close`
        self._lock = threading.Lock()
        self._items = 0
        self._bytes = 0  # offset of the end of the last complete line

    def append(self, text: str) -> None:
        """Append `text` (one JSON document without newline) as a line; flushed at once."""
        data = text.encode("utf-8") + b"\n"
        with self._lock:
            if self._bytes + len(data) > DATASET_MAX_BYTES:
                raise DatasetTooLargeError(f"the dataset would exceed {DATASET_MAX_BYTES} bytes")
            try:
                view = memoryview(data)
                while view:
                    view = view[self._fh.write(view) :]
            except BaseException:
                self._cut_to_last_line()
                raise
            self._items += 1
            self._bytes += len(data)

    def _cut_to_last_line(self) -> None:
        self._fh.truncate(self._bytes)
        self._fh.seek(self._bytes)

    def finalise(self) -> None:
        """Drop anything after the last complete line (a write that was interrupted)."""
        with self._lock:
            self._cut_to_last_line()

    def stats(self) -> dict[str, int]:
        """Complete lines and their bytes."""
        with self._lock:
            return {"items": self._items, "bytes": self._bytes}

    def close(self) -> None:
        self._fh.close()


class DatasetSink:
    """`ctx.dataset`: push items to the dataset file as the script finds them.

    `item_type` is the `Item` of the entry point's `Dataset[Item]` return annotation, or `None`
    when the entry point does not return a dataset.
    """

    def __init__(self, file: DatasetFile | None = None, item_type: Any = None) -> None:
        self._file = file
        self._adapter = None if item_type is None else TypeAdapter(item_type)
        self._item_type = item_type

    def push(self, item: Any) -> None:
        """Validate `item` against the dataset's item model and append it as one line.

        Raises `DatasetItemInvalidError` for an invalid item and `DatasetTooLargeError` when the
        line would take the file over `DATASET_MAX_BYTES`; earlier lines stay.
        """
        if self._file is None:
            raise NotAvailableError(
                "dataset.push", "is not available in this run: the platform gave no dataset file"
            )
        if self._adapter is None:
            raise NotAvailableError(
                "dataset.push", "is only available when the entry point returns Dataset[Item]"
            )
        try:
            validated = revalidate(self._adapter, item)
        except ValidationError as exc:
            raise DatasetItemInvalidError(
                "the pushed item does not match the dataset item model",
                validation_details(exc, self._item_type),
            ) from None
        self._file.append(self._adapter.dump_json(validated, by_alias=True).decode())
