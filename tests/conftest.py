from __future__ import annotations

import json
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mobilerun_sandbox import cli
from mobilerun_sandbox.entry import _ENTRY_POINTS


@pytest.fixture(autouse=True)
def clean_registry():
    _ENTRY_POINTS.clear()
    yield
    _ENTRY_POINTS.clear()


@pytest.fixture
def write_script(tmp_path: Path) -> Callable[[str], Path]:
    def _write(source: str) -> Path:
        path = tmp_path / "script.py"
        path.write_text(textwrap.dedent(source))
        return path

    return _write


@dataclass
class RunResult:
    code: int
    envelope: dict[str, Any] | None
    out: str
    err: str


@pytest.fixture
def run_script(tmp_path: Path, write_script, capsys) -> Callable[..., RunResult]:
    def _run(
        source: str,
        input_data: Any = None,
        *,
        raw_input: str | None = None,
        state: Any = ...,
        extra_args: list[str] | None = None,
    ) -> RunResult:
        script_path = write_script(source)
        input_file = tmp_path / "in.json"
        input_file.write_text(raw_input if raw_input is not None else json.dumps(input_data or {}))
        result_file = tmp_path / "out.json"
        result_file.unlink(missing_ok=True)
        argv = [
            "run",
            str(script_path),
            "--input-file",
            str(input_file),
            "--result-file",
            str(result_file),
        ]
        if state is not ...:
            state_file = tmp_path / "state.json"
            state_file.write_text(json.dumps(state))
            argv += ["--state-file", str(state_file)]
        argv += extra_args or []
        code = cli.main(argv)
        captured = capsys.readouterr()
        envelope = json.loads(result_file.read_text()) if result_file.exists() else None
        return RunResult(code, envelope, captured.out, captured.err)

    return _run
