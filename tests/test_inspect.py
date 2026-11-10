import json

from mobilerun_sandbox import cli

SAMPLE = """
from pydantic import BaseModel
from mobilerun_sandbox import Context, script

class Address(BaseModel):
    city: str

class In(BaseModel):
    name: str
    count: int = 1
    address: Address

class Out(BaseModel):
    greeting: str

@script
def main(input: In, ctx: Context) -> Out:
    return Out(greeting=input.name)
"""


def inspect(write_script, capsys, source):
    code = cli.main(["inspect", str(write_script(source))])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_inspect_sample(write_script, capsys):
    code, out, err = inspect(write_script, capsys, SAMPLE)
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


def test_inspect_async_and_no_output(write_script, capsys):
    code, out, _ = inspect(
        write_script,
        capsys,
        """
from pydantic import BaseModel
from mobilerun_sandbox import script

class In(BaseModel):
    x: int

@script
async def main(input: In) -> None:
    pass
""",
    )
    doc = json.loads(out)
    assert code == 0 and doc["async"] is True
    assert doc["outputSchema"] is None and doc["outputKind"] == "none"


def test_inspect_dataset_output_kind(write_script, capsys):
    code, out, _ = inspect(
        write_script,
        capsys,
        """
from pydantic import BaseModel
from mobilerun_sandbox import Dataset, script

class In(BaseModel):
    x: int

class Item(BaseModel):
    id: str

@script
def main(input: In) -> Dataset[Item]:
    return Dataset(items=[])
""",
    )
    doc = json.loads(out)
    assert code == 0 and doc["outputKind"] == "dataset"
    props = doc["outputSchema"]["properties"]
    assert props["items"]["items"]["properties"] == {"id": {"type": "string"}}
    assert "complete" in props
    assert "$defs" not in json.dumps(doc)


def _rejects(write_script, capsys, field_source, expected):
    code, out, err = inspect(
        write_script,
        capsys,
        f"""
from datetime import datetime
from pydantic import BaseModel, Field
from mobilerun_sandbox import script

class In(BaseModel):
    ok: str
    {field_source}

@script
def main(input: In) -> None:
    pass
""",
    )
    assert code == 3 and out == ""
    assert expected in err and err.count("\n") == 1


def test_inspect_rejects_pattern(write_script, capsys):
    _rejects(write_script, capsys, "code: str = Field(pattern='^a+$')", "field 'code'")


def test_inspect_rejects_format(write_script, capsys):
    _rejects(write_script, capsys, "when: datetime", "field 'when'")


def test_inspect_rejects_nested_pattern_with_path(write_script, capsys):
    code, _, err = inspect(
        write_script,
        capsys,
        """
from pydantic import BaseModel, Field
from mobilerun_sandbox import script

class Inner(BaseModel):
    slug: str = Field(pattern='^[a-z]+$')

class In(BaseModel):
    items: list[Inner]

@script
def main(input: In) -> None:
    pass
""",
    )
    assert code == 3 and "'items[].slug'" in err and "'pattern'" in err


def test_inspect_rejects_recursion(write_script, capsys):
    code, _, err = inspect(
        write_script,
        capsys,
        """
from __future__ import annotations
from pydantic import BaseModel
from mobilerun_sandbox import script

class Node(BaseModel):
    children: list[Node] = []

@script
def main(input: Node) -> None:
    pass
""",
    )
    assert code == 3 and "recursive" in err


def test_inspect_rejects_discriminator(write_script, capsys):
    code, _, err = inspect(
        write_script,
        capsys,
        """
from typing import Annotated, Literal
from pydantic import BaseModel, Field
from mobilerun_sandbox import script

class A(BaseModel):
    kind: Literal["a"]

class B(BaseModel):
    kind: Literal["b"]

class In(BaseModel):
    item: Annotated[A | B, Field(discriminator="kind")]

@script
def main(input: In) -> None:
    pass
""",
    )
    assert code == 3 and "'discriminator'" in err


def test_property_named_like_keyword_is_fine(write_script, capsys):
    code, out, _ = inspect(
        write_script,
        capsys,
        """
from pydantic import BaseModel
from mobilerun_sandbox import script

class In(BaseModel):
    pattern: str
    format: str
    title: str

@script
def main(input: In) -> None:
    pass
""",
    )
    assert code == 0
    assert set(json.loads(out)["inputSchema"]["properties"]) == {"pattern", "format", "title"}


def test_inspect_no_entry_point(write_script, capsys):
    code, out, err = inspect(write_script, capsys, "x = 1\n")
    assert code == 3 and out == "" and "no entry point" in err


def test_inspect_wrong_signature(write_script, capsys):
    code, out, err = inspect(
        write_script,
        capsys,
        """
from mobilerun_sandbox import script

@script
def main(input):
    pass
""",
    )
    assert code == 3 and "'input'" in err and err.count("\n") == 1


def test_inspect_import_error(write_script, capsys):
    code, _, err = inspect(write_script, capsys, "import nonexistent_module_xyz\n")
    assert code == 3 and "ModuleNotFoundError" in err


def test_inspect_missing_file(tmp_path, capsys):
    assert cli.main(["inspect", str(tmp_path / "nope.py")]) == 3
    assert "FileNotFoundError" in capsys.readouterr().err
