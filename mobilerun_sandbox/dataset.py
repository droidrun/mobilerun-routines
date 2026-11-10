"""`Dataset`, `ctx.dataset.push` and the JSON Lines file behind it."""

from __future__ import annotations

import os
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

    Exactly one field of an item model carries it. Other arguments go to `Field` unchanged;
    a `json_schema_extra` (dict or callable) is kept next to the marker.
    """
    extra = kwargs.pop("json_schema_extra", None)
    if extra is None or isinstance(extra, dict):
        return Field(json_schema_extra={**(extra or {}), KEY_MARKER: True}, **kwargs)

    def mark(schema: dict[str, Any]) -> None:
        extra(schema)
        schema[KEY_MARKER] = True

    return Field(json_schema_extra=mark, **kwargs)


class DatasetFile:
    """The JSON Lines file of one run: created empty at start, one complete line per item."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fh = open(self.path, "wb", buffering=0)  # noqa: SIM115 - closed in `close`
        self._bytes = 0

    def append(self, text: str) -> None:
        """Append `text` (one JSON document without newline) as a line; flushed at once."""
        data = text.encode("utf-8") + b"\n"
        if self._bytes + len(data) > DATASET_MAX_BYTES:
            raise DatasetTooLargeError(DATASET_MAX_BYTES)
        view = memoryview(data)
        while view:
            view = view[self._fh.write(view) :]
        self._bytes += len(data)

    def stats(self) -> dict[str, int]:
        """Lines and bytes on disk; counted from the file so an interrupted write cannot skew it."""
        data = self.path.read_bytes()
        return {"items": data.count(b"\n"), "bytes": len(data)}

    def close(self) -> None:
        self._fh.close()


class DatasetSink:
    """`ctx.dataset`: push items to the dataset file as the script finds them."""

    def __init__(
        self,
        file: DatasetFile | None = None,
        item_type: Any = None,
        unavailable: str = "is not available in this run: the platform gave no dataset file",
    ) -> None:
        self._file = file
        self._item_type = item_type
        self._unavailable = unavailable

    def push(self, item: Any) -> None:
        """Validate `item` against the dataset's item model and append it as one line.

        Raises `DatasetItemInvalidError` for an invalid item and `DatasetTooLargeError` when the
        line would take the file over `DATASET_MAX_BYTES`; earlier lines stay.
        """
        if self._file is None:
            raise NotAvailableError("dataset.push", self._unavailable)
        item_type = self._item_type
        try:
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                text = revalidate(item_type, item).model_dump_json(by_alias=True)
            else:
                adapter = TypeAdapter(item_type)
                text = adapter.dump_json(adapter.validate_python(item), by_alias=True).decode()
        except ValidationError as exc:
            raise DatasetItemInvalidError(
                "the pushed item does not match the dataset item model",
                validation_details(exc, item_type),
            ) from None
        self._file.append(text)
