"""`Dataset`: a list result the platform can diff between runs."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

ItemT = TypeVar("ItemT")


class Dataset(BaseModel, Generic[ItemT]):
    """Return `Dataset[Item]` to publish a list of items; `complete=False` marks a partial list."""

    items: list[ItemT]
    complete: bool = True
