"""Validation helpers shared by the runner and the dataset: re-validation and safe details."""

from __future__ import annotations

import typing
from functools import cache
from typing import Any

from pydantic import BaseModel, ValidationError

MAX_ERROR_DETAILS = 50


def revalidate(model: type[BaseModel], value: Any) -> BaseModel:
    """Validate `value` against `model`; instances are checked again, not trusted.

    An instance may come from `model_construct` or have been changed after construction, so it
    is dumped by field name and validated again. `by_name` makes that work for fields with
    `alias`, `validation_alias` and `serialization_alias`. Raises `ValidationError`.
    """
    if isinstance(value, BaseModel):
        data = value.model_dump(by_alias=False, warnings=False)
        return model.model_validate(data, by_name=True)
    return model.model_validate(value)


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


def validation_details(exc: ValidationError, model: Any) -> list[dict[str, str]]:
    """Declared field names, list indexes and error types only; never input values or keys."""
    declared = _declared_names(model) if isinstance(model, type) else frozenset()

    def part(component: Any) -> str:
        if isinstance(component, int) or component in declared:
            return str(component)
        return "*"

    return [
        {"path": ".".join(part(c) for c in err["loc"]), "message": err["type"]}
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ][:MAX_ERROR_DETAILS]
