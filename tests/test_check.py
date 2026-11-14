"""Static script checks in `inspect`: codes, lines and repair hints."""

import json
import textwrap

import pytest

from mobilerun_routines._check import SCRIPT_HINT, check_source

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


def test_the_hint_names_the_import_and_the_decorator():
    assert "from mobilerun_routines import Context, script" in SCRIPT_HINT
    assert "@script" in SCRIPT_HINT
    assert "def main(input: Input, ctx: Context) -> Output:" in SCRIPT_HINT


def test_a_script_without_a_decorator(inspect_source):
    source = _source(GOOD_IMPORT, MODELS, "def main(input: Input, ctx: Context) -> Output: ...")
    doc, err = _failure(inspect_source, source)
    assert _codes(doc) == [("no_entry_point", None)]
    assert doc["error"]["repair"] == {"action": "fix_script", "hint": SCRIPT_HINT}
    assert err == f"no entry point: mark one function with @script — {SCRIPT_HINT}\n"


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


def test_every_problem_is_reported_at_once_ordered_by_line(inspect_source):
    source = _source("from mobilerun import script, Context", MODELS, "def main(): ...")
    doc, err = _failure(inspect_source, source)
    assert _codes(doc) == [("import_wrong_module", 1), ("import_wrong_module", 1),
                           ("no_entry_point", None)]
    assert err.count("\n") == 3


def test_syntax_error_has_a_line_and_stops_the_checks(inspect_source):
    source = _source(GOOD_IMPORT, "@script\ndef main(:\n    pass")
    doc, err = _failure(inspect_source, source)
    line = source.splitlines().index("def main(:") + 1
    assert _codes(doc) == [("syntax_error", line)]
    assert doc["error"]["repair"]["hint"] == f"Fix the syntax error on line {line}."
    assert err.startswith(f"line {line}: syntax error: ")


def test_a_compile_stage_syntax_error_is_found_statically(inspect_source):
    doc, _ = _failure(inspect_source, "return 1\n")
    assert _codes(doc) == [("syntax_error", 1)]


def test_the_checks_never_run_the_script(inspect_source, tmp_path):
    marker = tmp_path / "ran"
    inspect_source(f"open({str(marker)!r}, 'w').close()\n")
    assert not marker.exists()


def test_a_duplicate_entry_point_is_reported_with_the_loader_message(inspect_source):
    source = _source(
        GOOD_IMPORT, MODELS, "@script\n" + BODY, "@script\ndef other(input: Input) -> None: ..."
    )
    doc, _ = _failure(inspect_source, source)
    line = source.splitlines().index("def other(input: Input) -> None: ...")
    assert _codes(doc) == [("entry_point_invalid", line)]
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


def test_check_source_accepts_bytes_and_passes_a_good_script():
    assert check_source(_source(GOOD_IMPORT, MODELS, "@script\n" + BODY).encode(), "x.py") == []


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
