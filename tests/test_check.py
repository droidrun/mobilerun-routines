"""Static script checks in `inspect`: codes, lines and repair hints."""

import json
import textwrap

import pytest

from mobilerun_routines._check import SCRIPT_HINT

MODELS = """\
from pydantic import BaseModel


class Input(BaseModel):
    name: str = "world"


class Output(BaseModel):
    message: str
"""

BODY = """\
def main(input: Input, ctx: Context) -> Output:
    return Output(message=input.name)
"""

GOOD_IMPORT = "from mobilerun_routines import Context, script\n"


def _source(*parts: str) -> str:
    return "\n\n".join(textwrap.dedent(part) for part in parts)


def _failure(inspect_source, source: str):
    code, out, err = inspect_source(source)
    assert code == 3
    doc = json.loads(out)
    assert doc["ok"] is False and doc["warnings"] == []
    assert doc["error"] == doc["errors"][0]
    return doc, err


def _codes(doc) -> list[tuple[str, int | None]]:
    return [(e["code"], e.get("line")) for e in doc["errors"]]


def test_a_script_without_a_decorator(inspect_source):
    source = _source(GOOD_IMPORT, MODELS, "def main(input: Input, ctx: Context) -> Output: ...")
    doc, err = _failure(inspect_source, source)
    assert _codes(doc) == [("no_entry_point", None)]
    # The hint text is the contract: the correct import and a one-line example.
    hint = (
        "Add `from mobilerun_routines import Context, script` and decorate one function: "
        "`@script` `def main(input: Input, ctx: Context) -> Output:`"
    )
    assert doc["error"]["repair"] == {"action": "fix_script", "hint": hint}
    assert err == f"no entry point: mark one function with @script — {hint}\n"


def test_a_decorator_without_the_import(inspect_source):
    source = _source(MODELS, "from mobilerun_routines import Context", "@script\n" + BODY)
    doc, err = _failure(inspect_source, source)
    line = source.splitlines().index("@script") + 1
    assert _codes(doc) == [("import_missing", line)]
    assert doc["error"]["repair"]["hint"] == SCRIPT_HINT
    assert err.startswith(f"line {line}: ") and err.endswith(f"{SCRIPT_HINT}\n")


def test_import_missing_is_reported_once_for_a_called_decorator(inspect_source):
    source = _source(MODELS, "from mobilerun_routines import Context", "@script()\n" + BODY)
    doc, _ = _failure(inspect_source, source)
    assert [e["code"] for e in doc["errors"]] == ["import_missing"]


@pytest.mark.parametrize("module", ["mobilerun", "mobilerun_sandbox", "mobilerun_sdk"])
def test_script_imported_from_the_wrong_module(inspect_source, module):
    source = _source(f"from {module} import script", MODELS, "@script\n" + BODY)
    doc, _ = _failure(inspect_source, source)
    # `Context` is not imported either: the import error is the only static finding.
    assert _codes(doc) == [("import_wrong_module", 1)]
    assert module in doc["error"]["message"]
    assert doc["error"]["repair"]["hint"] == SCRIPT_HINT


def test_context_imported_from_the_wrong_module(inspect_source):
    source = _source(
        "from mobilerun_routines import script\nfrom mobilerun_sdk import Context",
        MODELS,
        "@script\n" + BODY,
    )
    doc, _ = _failure(inspect_source, source)
    assert _codes(doc) == [("import_wrong_module", 2)]


def test_every_finding_is_reported_at_once_ordered_by_line(inspect_source):
    source = _source("from mobilerun import script, Context", MODELS, "@script\n" + BODY)
    doc, err = _failure(inspect_source, source)
    assert _codes(doc) == [("import_wrong_module", 1), ("import_wrong_module", 1)]
    assert err.count("\n") == 2


def test_findings_on_other_lines_are_reported_with_the_one_that_explains_the_failure(
    inspect_source,
):
    # The loader fails at line 1 (the wrong module); `script` is also never imported.
    source = _source("from mobilerun_sdk import Context", MODELS, "@script\n" + BODY)
    doc, _ = _failure(inspect_source, source)
    decorator = source.splitlines().index("@script") + 1
    assert _codes(doc) == [("import_wrong_module", 1), ("import_missing", decorator)]


def test_syntax_error_has_a_line(inspect_source):
    source = _source(GOOD_IMPORT, "@script\ndef main(:\n    pass")
    doc, err = _failure(inspect_source, source)
    line = source.splitlines().index("def main(:") + 1
    assert _codes(doc) == [("syntax_error", line)]
    assert doc["error"]["repair"]["hint"] == f"Fix the syntax error on line {line}."
    assert err.startswith(f"line {line}: syntax error: ")


def test_a_compile_stage_syntax_error_is_a_syntax_error(inspect_source):
    doc, _ = _failure(inspect_source, "return 1\n")
    assert _codes(doc) == [("syntax_error", 1)]


def test_a_duplicate_entry_point_is_the_loaders_failure_with_the_line(inspect_source):
    source = _source(
        GOOD_IMPORT, MODELS, "@script\n" + BODY, "@script\ndef other(input: Input) -> None: ..."
    )
    doc, _ = _failure(inspect_source, source)
    line = source.splitlines().index("def other(input: Input) -> None: ...")
    assert _codes(doc) == [("entry_point_invalid", line)]  # the decorator line
    assert "duplicate entry point 'other': 'main'" in doc["error"]["message"]


@pytest.mark.parametrize(
    "header",
    [
        "from mobilerun_routines import Context, script",
        "from mobilerun_routines import Context, script as marker",
        "import mobilerun_routines as mr\nfrom mobilerun_routines import Context",
        "from mobilerun_routines import *",
    ],
)
def test_valid_imports_pass(inspect_source, header):
    name = "marker" if "as marker" in header else "mr.script" if "as mr" in header else "script"
    source = _source(header, MODELS, f"@{name}\n" + BODY)
    code, out, err = inspect_source(source)
    assert code == 0, err
    assert json.loads(out)["warnings"] == []


@pytest.mark.parametrize(
    "header",
    [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from mobilerun import script\n"
        "from mobilerun_routines import Context, script",
        "from mobilerun_routines import Context, script\nentry = script",
        "import functools\nfrom mobilerun_routines import Context, script\nother = functools",
    ],
    ids=["type-checking-import", "alias-assignment", "unrelated-attribute"],
)
def test_valid_patterns_the_loader_accepts_are_never_rejected(inspect_source, header):
    decorator = "@entry" if "entry = script" in header else "@script"
    source = _source(header, MODELS, f"{decorator}\n" + BODY)
    code, _, err = inspect_source(source)
    assert code == 0, err


def test_an_unrelated_attribute_decorator_is_accepted(inspect_source):
    source = _source(
        "import functools\nfrom mobilerun_routines import Context, script",
        MODELS,
        "class Other:\n    script = staticmethod(lambda f: f)",
        "@script\n" + BODY,
        "@Other.script\ndef helper() -> None: ...",
    )
    code, _, err = inspect_source(source)
    assert code == 0, err


def test_a_name_script_bound_locally_is_not_import_missing(inspect_source):
    source = _source(
        MODELS,
        "from mobilerun_routines import Context",
        "def run(script):\n    return script\n\nfrom mobilerun_routines import script as marker",
        "@marker\n" + BODY,
    )
    code, _, err = inspect_source(source)
    assert code == 0, err


def test_the_decorator_applied_as_a_call_is_an_entry_point(inspect_source):
    source = _source(GOOD_IMPORT, MODELS, BODY, "script(main)")
    code, _, err = inspect_source(source)
    assert code == 0, err


def test_the_starter_passes_with_no_warnings(tmp_path, capsys):
    from mobilerun_routines import cli

    path = tmp_path / "hello.py"
    assert cli.main(["new", str(path)]) == 0
    capsys.readouterr()
    assert cli.main(["inspect", str(path)]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["warnings"] == []


# Runtime import failures that get the same hint


def test_runtime_name_error_for_script_gets_the_hint_and_the_line(inspect_source):
    # The import exists, but where it never runs: the static check cannot tell.
    source = _source(
        MODELS,
        "from mobilerun_routines import Context\n\n"
        "def setup():\n    from mobilerun_routines import script",
        "@script\n" + BODY,
    )
    doc, _ = _failure(inspect_source, source)
    error = doc["error"]
    assert error["code"] == "import_failed" and "name 'script' is not defined" in error["message"]
    assert error["line"] == source.splitlines().index("@script") + 1
    assert error["repair"] == {"action": "fix_script", "hint": SCRIPT_HINT}


def test_runtime_name_error_for_context_gets_the_hint(inspect_source):
    source = _source("from mobilerun_routines import script", MODELS, "@script\n" + BODY)
    doc, _ = _failure(inspect_source, source)
    error = doc["error"]
    # The loader reports unresolved annotations as an invalid entry point.
    assert error["code"] == "entry_point_invalid"
    assert "name 'Context' is not defined" in error["message"]
    assert error["line"] is not None
    assert error["repair"]["hint"] == SCRIPT_HINT


@pytest.mark.parametrize("module", ["mobilerun", "mobilerun_sandbox", "mobilerun_sdk"])
def test_runtime_module_not_found_for_the_wrong_modules_gets_the_hint(inspect_source, module):
    # An import the static check does not look at: inside a function called at import time.
    source = _source(
        GOOD_IMPORT,
        MODELS,
        "@script\n" + BODY,
        f"def helper():\n    import {module}\n\nhelper()",
    )
    doc, _ = _failure(inspect_source, source)
    error = doc["error"]
    assert error["code"] == "import_failed" and "ModuleNotFoundError" in error["message"]
    assert error["line"] == source.splitlines().index(f"    import {module}") + 1
    assert error["repair"] == {"action": "fix_script", "hint": SCRIPT_HINT}


def test_other_import_failures_have_a_line_and_no_hint(inspect_source):
    source = _source(GOOD_IMPORT, MODELS, "@script\n" + BODY, "import nonexistent_module_xyz")
    doc, _ = _failure(inspect_source, source)
    assert "repair" not in doc["error"]
    assert doc["error"]["line"] == source.splitlines().index("import nonexistent_module_xyz") + 1
