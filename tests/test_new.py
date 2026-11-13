"""`new` writes a starter script that passes `inspect` and never overwrites."""

import json

from mobilerun_sandbox import cli
from mobilerun_sandbox.runner import EXIT_OK, EXIT_RUNNER_FAULT


def test_new_writes_a_script_that_inspect_accepts(tmp_path, capsys):
    path = tmp_path / "hello.py"
    assert cli.main(["new", str(path)]) == EXIT_OK
    assert capsys.readouterr().out == f"{path}\n"
    assert cli.main(["inspect", str(path)]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["outputKind"] == "value"
    assert set(doc["inputSchema"]["properties"]) == {"name", "greeting"}
    assert doc["inputSchema"]["required"] == ["name"]


def test_new_runs(tmp_path):
    script = tmp_path / "hello.py"
    cli.main(["new", str(script)])
    (tmp_path / "in.json").write_text('{"name": "Ada"}')
    code = cli.main(
        ["run", str(script), "--input-file", str(tmp_path / "in.json"),
         "--result-file", str(tmp_path / "out.json")]
    )  # fmt: skip
    assert code == EXIT_OK
    assert json.loads((tmp_path / "out.json").read_text())["result"] == {"message": "Hello, Ada!"}


def test_new_never_overwrites(tmp_path, capsys):
    path = tmp_path / "hello.py"
    path.write_text("keep")
    assert cli.main(["new", str(path)]) == EXIT_RUNNER_FAULT
    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err.splitlines()) == 1
    assert path.read_text() == "keep"


def test_new_creates_no_directories(tmp_path, capsys):
    path = tmp_path / "missing" / "hello.py"
    assert cli.main(["new", str(path)]) == EXIT_RUNNER_FAULT
    assert len(capsys.readouterr().err.splitlines()) == 1
    assert not path.parent.exists()
