"""JSON Schema for inputs and outputs, restricted to a small keyword subset."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic.errors import PydanticInvalidForJsonSchema, PydanticUserError
from pydantic.json_schema import JsonSchemaMode

REJECTED_KEYWORDS = ("pattern", "patternProperties", "format", "prefixItems", "discriminator")
# Keywords whose value is a map of name -> schema.
_SCHEMA_MAPS = ("properties", "patternProperties", "dependentSchemas")
# Keywords whose value is one schema, with the path suffix that names the position.
_SCHEMA_ONE = {
    "items": "[]",
    "additionalProperties": "{}",
    "propertyNames": "{key}",
    "not": "",
    "if": "",
    "then": "",
    "else": "",
    "contains": "",
    "unevaluatedItems": "[]",
    "unevaluatedProperties": "{}",
}
# Keywords whose value is a list of schemas.
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")


class SchemaError(Exception):
    """The model has no JSON Schema in the supported keyword subset."""


def _where(path: str) -> str:
    return f"field '{path}'" if path else "the root schema"


def _walk(node: Any, defs: dict[str, Any], stack: tuple[str, ...], path: str) -> Any:
    if not isinstance(node, dict):
        return node
    node = dict(node)
    ref = node.pop("$ref", None)
    out: dict[str, Any] = {}
    if ref is not None:
        name = ref.rsplit("/", 1)[-1]
        if name in stack:
            raise SchemaError(f"{_where(path)}: recursive model '{name}' is not supported")
        if name not in defs:
            raise SchemaError(f"{_where(path)}: unresolved reference '{ref}'")
        out = _walk(defs[name], defs, (*stack, name), path)
    for key, value in node.items():
        if key in REJECTED_KEYWORDS:
            raise SchemaError(f"{_where(path)}: keyword '{key}' is not supported")
        if key in ("title", "$defs", "definitions"):
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {
                name: _walk(sub, defs, stack, f"{path}.{name}" if path else name)
                for name, sub in value.items()
            }
        elif key in _SCHEMA_ONE and isinstance(value, dict):
            out[key] = _walk(value, defs, stack, path + _SCHEMA_ONE[key])
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [_walk(sub, defs, stack, path) for sub in value]
        else:
            out[key] = value
    return out


def model_schema(model: type[BaseModel], mode: JsonSchemaMode) -> dict[str, Any]:
    """Inlined, title-free JSON Schema of `model`; raises `SchemaError` outside the subset."""
    try:
        raw = model.model_json_schema(mode=mode)
    except (PydanticInvalidForJsonSchema, PydanticUserError) as exc:
        raise SchemaError(f"cannot build JSON Schema: {str(exc).splitlines()[0]}") from exc
    defs = {**raw.get("definitions", {}), **raw.get("$defs", {})}
    return _walk(raw, defs, (), "")


def input_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model_schema(model, "validation")


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model_schema(model, "serialization")
