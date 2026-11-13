"""JSON Schema for inputs and outputs, restricted to a small keyword subset."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic.errors import PydanticInvalidForJsonSchema, PydanticUserError
from pydantic.json_schema import JsonSchemaMode

from .dataset import KEY_MARKER

REJECTED_KEYWORDS = ("pattern", "patternProperties", "prefixItems", "discriminator")
# The `format` values the platform validates (with `ajv-formats`).
ALLOWED_FORMATS = ("date-time", "date", "uuid", "uri", "email")
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
        if key == "format" and value not in ALLOWED_FORMATS:
            raise SchemaError(f"{_where(path)}: format '{value}' is not supported")
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


def dataset_key(schema: dict[str, Any]) -> str:
    """Name of the one item field marked with `key_field` in the schema of a `Dataset[Item]`.

    The name is the serialization name, as it appears in the dataset lines. Raises
    `SchemaError` without exactly one key or when the key does not serialise to a JSON string or
    integer (so a `UUID` is fine and a `float` is not).
    """
    items = schema.get("properties", {}).get("items", {}).get("items", {})
    properties = items.get("properties", {}) if isinstance(items, dict) else {}
    keys = [name for name, prop in properties.items() if prop.get(KEY_MARKER) is True]
    if not keys:
        raise SchemaError("dataset items have no key: mark one field with key_field()")
    if len(keys) > 1:
        raise SchemaError(f"dataset items have more than one key: {', '.join(keys)}")
    if properties[keys[0]].get("type") not in ("string", "integer"):
        raise SchemaError(f"the key field '{keys[0]}' must serialise to a string or an integer")
    return keys[0]


def input_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model_schema(model, "validation")


def output_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model_schema(model, "serialization")
