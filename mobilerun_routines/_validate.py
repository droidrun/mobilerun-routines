"""Validation helpers shared by the runner and the dataset: re-validation and safe details."""

from __future__ import annotations

import typing
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

MAX_ERROR_DETAILS = 50


def _field_data(value: Any) -> Any:
    """Plain data holding only what was set on the declared fields of `value`, by field name.

    Models are unpacked recursively. Computed fields are output, not input, so they are left out
    and appear again when the validated model is serialised.
    """
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        data = {name: _field_data(v) for name, v in value.__dict__.items() if name in fields}
        data.update({name: _field_data(v) for name, v in (value.model_extra or {}).items()})
        return data
    if isinstance(value, list | tuple | set | frozenset):
        return type(value)(_field_data(v) for v in value)
    if isinstance(value, dict):
        return {key: _field_data(v) for key, v in value.items()}
    return value


def revalidate(adapter: TypeAdapter[Any], value: Any) -> Any:
    """Validate `value`; model instances are checked again, not trusted.

    An instance may come from `model_construct` or have been changed after construction. Its
    data is read by field name and validated by field name only: alias lookup would mix up
    fields whose names are also aliases of other fields. (`by_name` needs pydantic 2.11.)
    Raises `ValidationError`.
    """
    if isinstance(value, BaseModel):
        return adapter.validate_python(_field_data(value), by_alias=False, by_name=True)
    return adapter.validate_python(value)


def _declared_names(model: Any) -> frozenset[str]:
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
    declared = _declared_names(model)

    def part(component: Any) -> str:
        if isinstance(component, int) or component in declared:
            return str(component)
        return "*"

    return [
        {"path": ".".join(part(c) for c in err["loc"]), "message": err["type"]}
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ][:MAX_ERROR_DETAILS]
