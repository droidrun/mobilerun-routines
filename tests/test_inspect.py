import json

import pytest
from script_source import script_source

from mobilerun_sandbox import cli

SAMPLE = script_source(
    "return Out(greeting=input.name)",
    models="""
    class Address(BaseModel):
        city: str

    class In(BaseModel):
        name: str
        count: int = 1
        address: Address

    class Out(BaseModel):
        greeting: str
    """,
)


def test_inspect_sample(inspect_source):
    code, out, err = inspect_source(SAMPLE)
    assert code == 0 and err == ""
    doc = json.loads(out)
    assert doc["entrypoint"] == "main" and doc["async"] is False
    assert doc["sdkVersion"] == "0.1.0" and doc["outputKind"] == "value"
    assert doc["inputSchema"] == {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer", "default": 1},
            "address": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
        "required": ["name", "address"],
    }
    assert doc["outputSchema"] == {
        "type": "object",
        "properties": {"greeting": {"type": "string"}},
        "required": ["greeting"],
    }


def test_inspect_async_and_no_output(inspect_source):
    code, out, _ = inspect_source(script_source(ret=None, sig="input: In", is_async=True))
    doc = json.loads(out)
    assert code == 0 and doc["async"] is True
    assert doc["outputSchema"] is None and doc["outputKind"] == "none"


def test_inspect_dataset_output_kind(inspect_source):
    code, out, _ = inspect_source(
        script_source(
            "return Dataset(items=[])",
            sig="input: In",
            ret="Dataset[Item]",
            before="""
            class Item(BaseModel):
                id: str
            """,
        )
    )
    doc = json.loads(out)
    assert code == 0 and doc["outputKind"] == "dataset"
    props = doc["outputSchema"]["properties"]
    assert props["items"]["items"]["properties"] == {"id": {"type": "string"}}
    assert "complete" in props
    assert "$defs" not in json.dumps(doc)


def _with_model(models: str, ret: str = "None") -> str:
    return script_source(sig="input: In", ret=ret, models=models)


REJECTED = {
    "pattern": ("code: str = Field(pattern='^a+$')", "field 'code'", "'pattern'"),
    "format": ("when: datetime", "field 'when'", "'format'"),
    "date key": ("by_day: dict[date, int]", "field 'by_day", "'format'"),
    "discriminator": (
        "item: Annotated[A | B, Field(discriminator='kind')]",
        "field 'item'",
        "'discriminator'",
    ),
}

EXTRA_MODELS = """
class A(BaseModel):
    kind: Literal["a"]

class B(BaseModel):
    kind: Literal["b"]
"""


@pytest.mark.parametrize(("field", "where", "keyword"), REJECTED.values(), ids=REJECTED)
def test_inspect_rejects_keywords_outside_the_subset(inspect_source, field, where, keyword):
    models = f"{EXTRA_MODELS}\nclass In(BaseModel):\n    ok: str\n    {field}\n"
    code, out, err = inspect_source(_with_model(models))
    assert code == 3 and out == ""
    assert where in err and keyword in err and err.count("\n") == 1


def test_inspect_rejects_nested_pattern_with_path(inspect_source):
    models = """
    class Inner(BaseModel):
        slug: str = Field(pattern='^[a-z]+$')

    class In(BaseModel):
        items: list[Inner]
    """
    code, _, err = inspect_source(_with_model(models))
    assert code == 3 and "'items[].slug'" in err and "'pattern'" in err


def test_inspect_rejects_recursion(inspect_source):
    source = "from __future__ import annotations\n" + _with_model(
        """
            class In(BaseModel):
                children: list[In] = []
            """
    )
    code, _, err = inspect_source(source)
    assert code == 3 and "recursive" in err


def test_enum_dictionary_keys_are_inlined(inspect_source):
    models = """
    class Color(str, Enum):
        red = "red"
        blue = "blue"

    class In(BaseModel):
        votes: dict[Color, int]
    """
    code, out, _ = inspect_source(_with_model(models))
    assert code == 0
    names = json.loads(out)["inputSchema"]["properties"]["votes"]["propertyNames"]
    assert names == {"enum": ["red", "blue"], "type": "string"}
    assert "$ref" not in out and "$defs" not in out


def test_property_named_like_keyword_is_fine(inspect_source):
    models = """
    class In(BaseModel):
        pattern: str
        format: str
        title: str
    """
    code, out, _ = inspect_source(_with_model(models))
    assert code == 0
    assert set(json.loads(out)["inputSchema"]["properties"]) == {"pattern", "format", "title"}


def test_inspect_no_entry_point(inspect_source):
    code, out, err = inspect_source("x = 1\n")
    assert code == 3 and out == "" and "no entry point" in err


def test_inspect_wrong_signature(inspect_source):
    code, out, err = inspect_source(script_source(sig="input"))
    assert code == 3 and "'input'" in err and err.count("\n") == 1


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import nonexistent_module_xyz\n", "ModuleNotFoundError"),
        ("import sys\nsys.exit(0)\n", "SystemExit: 0"),
        ("open('missing-resource')\n", "FileNotFoundError"),
    ],
)
def test_inspect_import_time_failure(inspect_source, source, expected):
    code, out, err = inspect_source(source)
    assert code == 3 and out == "" and expected in err and err.count("\n") == 1


def test_inspect_missing_file(tmp_path, capsys):
    assert cli.main(["inspect", str(tmp_path / "nope.py")]) == 3
    assert "cannot read script" in capsys.readouterr().err
