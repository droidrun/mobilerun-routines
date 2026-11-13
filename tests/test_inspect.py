import json
import textwrap

import pytest
from pydantic import BaseModel
from script_source import script_source

from mobilerun_routines import cli, key_field
from mobilerun_routines._version import SDK_VERSION

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
    assert doc["ok"] is True and doc["datasetKey"] is None
    assert doc["entrypoint"] == "main" and doc["async"] is False
    assert doc["sdkVersion"] == SDK_VERSION == "0.4.0" and doc["outputKind"] == "value"
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
            from mobilerun_routines import key_field

            class Item(BaseModel):
                id: str = key_field()
            """,
        )
    )
    doc = json.loads(out)
    assert code == 0 and doc["outputKind"] == "dataset" and doc["datasetKey"] == "id"
    props = doc["outputSchema"]["properties"]
    assert props["items"]["items"]["properties"] == {
        "id": {"type": "string", "x-mobilerun-key": True}
    }
    assert set(props) == {"items"}  # no `complete`
    assert "$defs" not in json.dumps(doc)


def _with_model(models: str, ret: str = "None") -> str:
    return script_source(sig="input: In", ret=ret, models=models)


REJECTED = {
    "pattern": ("code: str = Field(pattern='^a+$')", "field 'code'", "'pattern'"),
    "time": ("at: time", "field 'at'", "format 'time'"),
    "ipv4": ("host: IPv4Address", "field 'host'", "format 'ipv4'"),
    "unknown format": (
        "x: str = Field(json_schema_extra={'format': 'hostname'})",
        "field 'x'",
        "format 'hostname'",
    ),
    "nested format": ("rows: list[Row]", "field 'rows[].at'", "format 'time'"),
    "discriminator": (
        "item: Annotated[A | B, Field(discriminator='kind')]",
        "field 'item'",
        "'discriminator'",
    ),
}

EXTRA_MODELS = """
from datetime import time
from ipaddress import IPv4Address

class Row(BaseModel):
    at: time

class A(BaseModel):
    kind: Literal["a"]

class B(BaseModel):
    kind: Literal["b"]
"""


@pytest.mark.parametrize(("field", "where", "keyword"), REJECTED.values(), ids=REJECTED)
def test_inspect_rejects_keywords_outside_the_subset(inspect_source, field, where, keyword):
    models = f"{EXTRA_MODELS}\nclass In(BaseModel):\n    ok: str\n    {field}\n"
    code, out, err = inspect_source(_with_model(models))
    assert code == 3
    assert where in err and keyword in err and err.count("\n") == 1
    assert json.loads(out)["error"]["code"] == "schema_unsupported"


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


def _failure(out: str) -> dict:
    doc = json.loads(out)
    assert doc["ok"] is False and set(doc) == {"ok", "error"}
    assert set(doc["error"]) == {"code", "message"}
    return doc["error"]


def test_inspect_no_entry_point(inspect_source):
    code, out, err = inspect_source("x = 1\n")
    assert code == 3 and "no entry point" in err and err.count("\n") == 1
    error = _failure(out)
    assert error["code"] == "no_entry_point" and "no entry point" in error["message"]


def test_inspect_wrong_signature(inspect_source):
    code, out, err = inspect_source(script_source(sig="input"))
    assert code == 3 and "'input'" in err and err.count("\n") == 1
    error = _failure(out)
    assert error["code"] == "entry_point_invalid" and "'input'" in error["message"]


def test_inspect_duplicate_entry_point(inspect_source):
    source = script_source() + "\n@script\ndef other(input: In) -> None:\n    pass\n"
    code, out, _ = inspect_source(source)
    assert code == 3 and _failure(out)["code"] == "entry_point_invalid"


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
    assert code == 3 and expected in err and err.count("\n") == 1
    error = _failure(out)
    assert error["code"] == "import_failed" and expected in error["message"]


def test_inspect_missing_file(tmp_path, capsys):
    assert cli.main(["inspect", str(tmp_path / "nope.py")]) == 3
    captured = capsys.readouterr()
    assert "cannot read script" in captured.err
    assert _failure(captured.out)["code"] == "import_failed"


def test_inspect_error_message_is_one_line_in_json(inspect_source):
    code, out, _ = inspect_source("raise RuntimeError('a\\nb')\n")
    assert code == 3 and "\n" not in _failure(out)["message"]


# D2: formats

FORMAT_CASES = {
    "date-time": ("when: datetime", "from datetime import datetime"),
    "date": ("day: date", "from datetime import date"),
    "uuid": ("ident: UUID", "from uuid import UUID"),
    "uri": ("link: AnyUrl", "from pydantic import AnyUrl"),
    "uri-http": ("link: HttpUrl", "from pydantic import HttpUrl"),
    "email": ("mail: str = Field(json_schema_extra={'format': 'email'})", ""),
}


@pytest.mark.parametrize("name", FORMAT_CASES)
def test_inspect_allows_the_declared_formats(inspect_source, name):
    field, imports = FORMAT_CASES[name]
    models = f"{imports}\nclass In(BaseModel):\n    {field}\n"
    code, out, err = inspect_source(_with_model(models, ret="In"))
    assert code == 0, err
    doc = json.loads(out)
    prop = next(iter(doc["inputSchema"]["properties"].values()))
    assert prop["format"] == ("uri" if name.startswith("uri") else name)
    assert prop["type"] == "string"
    assert set(doc["outputSchema"]["properties"]) == set(doc["inputSchema"]["properties"])


def test_run_validates_formatted_input_with_the_model(run_script):
    models = """
    from datetime import datetime
    from uuid import UUID

    class In(BaseModel):
        when: datetime
        ident: UUID

    class Out(BaseModel):
        text: str
    """
    body = (
        "names = [type(input.when).__name__, type(input.ident).__name__, str(input.when.year)]\n"
        'return Out(text=" ".join(names))'
    )
    ok = run_script(
        script_source(body, models=models),
        {"when": "2026-11-10T12:00:00Z", "ident": "12345678-1234-5678-1234-567812345678"},
    )
    assert ok.code == 0 and ok.envelope["result"] == {"text": "datetime UUID 2026"}
    bad = run_script(script_source(body, models=models), {"when": "yesterday", "ident": "x"})
    assert bad.code == 2 and bad.envelope["error"]["code"] == "input_invalid"
    assert "yesterday" not in json.dumps(bad.envelope)


# D3: dataset key


def _dataset(item_fields: str, *, extra: str = "") -> str:
    return script_source(
        "return Dataset(items=[])",
        sig="input: In",
        ret="Dataset[Item]",
        before=f"""
        from mobilerun_routines import key_field
        {extra}
        class Item(BaseModel):
{textwrap.indent(textwrap.dedent(item_fields), " " * 12)}
        """,
    )


def test_key_field_marks_the_field():
    class Item(BaseModel):
        id: str = key_field()
        other: str = key_field(description="d", json_schema_extra={"x-other": 1})
        plain: str = ""

    props = Item.model_json_schema()["properties"]
    assert props["id"]["x-mobilerun-key"] is True
    assert props["other"] == {
        "type": "string",
        "title": "Other",
        "description": "d",
        "x-other": 1,
        "x-mobilerun-key": True,
    }
    assert "x-mobilerun-key" not in props["plain"]


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ("id: str = key_field()\nprice: float = 0", "id"),
        ("sku: int = key_field()\nprice: float = 0", "sku"),
        ('sku: str = key_field(serialization_alias="skuId")', "skuId"),
        ('sku: str = key_field(alias="skuId")', "skuId"),
        ("price: float = 0\nsku: str = key_field()", "sku"),
    ],
    ids=["str", "int", "serialization-alias", "alias", "key-last"],
)
def test_inspect_reports_the_dataset_key(inspect_source, fields, key):
    code, out, err = inspect_source(_dataset(fields))
    assert code == 0, err
    doc = json.loads(out)
    assert doc["datasetKey"] == key
    assert key in doc["outputSchema"]["properties"]["items"]["items"]["properties"]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ("id: str\nprice: float = 0", "no key"),
        ("id: str = key_field()\nsku: str = key_field()", "more than one key"),
        ("id: bool = key_field()", "serialise to a string or an integer"),
        ("id: float = key_field()", "serialise to a string or an integer"),
        ("id: list[str] = key_field()", "serialise to a string or an integer"),
        ("id: str | None = key_field(default=None)", "serialise to a string or an integer"),
    ],
    ids=["none", "two", "bool", "float", "list", "optional"],
)
def test_inspect_rejects_a_bad_dataset_key(inspect_source, fields, expected):
    code, out, err = inspect_source(_dataset(fields))
    assert code == 3 and expected in err and err.count("\n") == 1
    error = _failure(out)
    assert error["code"] == "schema_unsupported" and expected in error["message"]


def test_inspect_rejects_a_dataset_of_non_models(inspect_source):
    source = script_source(
        "return Dataset(items=[])", sig="input: In", ret="Dataset[int]"
    )
    code, out, _ = inspect_source(source)
    assert code == 3 and _failure(out)["code"] == "schema_unsupported"


def test_a_key_on_a_non_dataset_output_is_ignored(inspect_source):
    source = script_source(
        "return Out(greeting='x')",
        models="""
        from mobilerun_routines import key_field

        class In(BaseModel):
            pass

        class Out(BaseModel):
            greeting: str = key_field()
        """,
    )
    code, out, _ = inspect_source(source)
    doc = json.loads(out)
    assert code == 0 and doc["datasetKey"] is None and doc["outputKind"] == "value"


# Fix round 1: stdout is exactly one JSON document

def test_a_top_level_print_does_not_corrupt_inspect_output(inspect_source):
    code, out, err = inspect_source('print("user diagnostic")\n' + SAMPLE)
    assert code == 0
    assert json.loads(out)["ok"] is True  # json.loads fails on any extra line
    assert "user diagnostic" in err


def test_a_top_level_print_does_not_corrupt_an_inspect_error(inspect_source):
    code, out, err = inspect_source('print("user diagnostic")\nimport nonexistent_module_xyz\n')
    assert code == 3
    assert _failure(out)["code"] == "import_failed"
    assert "user diagnostic" in err


def test_a_uuid_key_is_accepted(inspect_source):
    source = _dataset("id: UUID = key_field()", extra="from uuid import UUID")
    code, out, err = inspect_source(source)
    assert code == 0, err
    assert json.loads(out)["datasetKey"] == "id"
