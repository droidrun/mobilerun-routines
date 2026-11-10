"""JSON Schema for inputs and outputs, restricted to a small keyword subset."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

REJECTED_KEYWORDS = ("pattern", "patternProperties", "format", "prefixItems", "discriminator")
# Values that are a map of name -> schema.
_SCHEMA_MAPS = ("properties", "$defs", "definitions")
# Values that are one schema.
_SCHEMA_ONE = ("items", "additionalProperties", "not", "contains", "if", "then", "else")
# Values that are a list of schemas.
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf")


class SchemaError(Exception):
    """The model's schema uses a keyword outside the supported subset."""


def _where(path: str) -> str:
    return f"field '{path}'" if path else "the root schema"


def _walk(node: Any, defs: dict[str, Any], stack: tuple[str, ...], path: str) -> Any:
    if not isinstance(node, dict):
        return node
    node = dict(node)
    ref = node.pop("$ref", None)
    base: dict[str, Any] = {}
    if ref is not None:
        name = ref.rsplit("/", 1)[-1]
        if name in stack:
            raise SchemaError(f"{_where(path)}: recursive model '{name}' is not supported")
        if name not in defs:
            raise SchemaError(f"{_where(path)}: unresolved reference '{ref}'")
        base = _walk(defs[name], defs, (*stack, name), path)
    out: dict[str, Any] = dict(base)
    for key, value in node.items():
        if key in REJECTED_KEYWORDS:
            raise SchemaError(f"{_where(path)}: keyword '{key}' is not supported")
        if key == "title" or key in ("$defs", "definitions"):
            continue
        if key == "properties":
            out[key] = {
                name: _walk(sub, defs, stack, f"{path}.{name}" if path else name)
                for name, sub in value.items()
            }
        elif key in _SCHEMA_ONE and isinstance(value, dict):
            sub_path = f"{path}[]" if key == "items" else path
            out[key] = _walk(value, defs, stack, sub_path)
        elif key in _SCHEMA_LISTS:
            out[key] = [_walk(sub, defs, stack, path) for sub in value]
        else:
            out[key] = value
    return out


def model_schema(model: type[BaseModel], mode: str) -> dict[str, Any]:
    """Inlined, title-free JSON Schema of `model`; raises `SchemaError` outside the subset."""
    raw = model.model_json_schema(mode=mode)  # type: ignore[arg-type]
    defs = {**raw.get("definitions", {}), **raw.get("$defs", {})}
    return _walk(raw, defs, (), "")


def input_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model_schema(model, "validation")


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model_schema(model, "serialization")
