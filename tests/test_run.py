import json
import subprocess
import sys

from mobilerun_sandbox import RESULT_MAX_BYTES, STATE_MAX_BYTES

HEADER = """
from pydantic import BaseModel
from mobilerun_sandbox import Context, Dataset, NotAvailableError, ScriptError, script

class In(BaseModel):
    name: str = "x"
    count: int = 1
    extra: int = 0

class Out(BaseModel):
    greeting: str
"""

GREETER = (
    HEADER
    + """
@script
def main(input: In, ctx: Context) -> Out:
    print("hello from script")
    return Out(greeting=f"hi {input.name} x{input.count}")
"""
)


def test_success(run_script):
    r = run_script(GREETER, {"name": "ada", "count": 2})
    assert r.code == 0
    assert r.envelope == {
        "ok": True,
        "outcome": "succeeded",
        "result": {"greeting": "hi ada x2"},
    }
    assert "hello from script" in r.out  # script stdout passes through, is not parsed


def test_script_error(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    raise ScriptError("login_failed", "bad password", retryable=True)
""",
    )
    assert r.code == 1
    assert r.envelope == {
        "ok": False,
        "error": {"code": "login_failed", "message": "bad password", "retryable": True},
    }


def test_script_error_defaults_to_not_retryable(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In) -> Out:
    raise ScriptError("nope", "no")
""",
    )
    assert r.envelope["error"]["retryable"] is False


def test_unexpected_exception(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    raise ValueError("boom " + "x" * 600)
""",
    )
    assert r.code == 1
    error = r.envelope["error"]
    assert r.envelope["ok"] is False
    assert error["code"] == "script_error" and error["retryable"] is False
    assert error["message"].startswith("ValueError: boom x")
    assert len(error["message"]) == len("ValueError: ") + 500
    assert "Traceback" in r.err


def test_sys_exit_in_script_is_a_script_error(run_script):
    r = run_script(
        HEADER
        + """
import sys

@script
def main(input: In) -> Out:
    sys.exit(3)
""",
    )
    assert r.code == 1 and r.envelope["error"]["message"] == "SystemExit: 3"


def test_exception_at_import_is_a_script_error(run_script):
    r = run_script("raise RuntimeError('at import')\n")
    assert r.code == 1
    assert r.envelope["error"]["message"] == "RuntimeError: at import"


def test_invalid_input_reports_paths_not_values(run_script):
    r = run_script(
        """
from pydantic import BaseModel
from mobilerun_sandbox import script

class Inner(BaseModel):
    n: int

class In(BaseModel):
    inner: Inner
    tags: list[int]

@script
def main(input: In) -> None:
    pass
""",
        {"inner": {"n": "SECRET-VALUE"}, "tags": [1, "SECRET-TWO"]},
    )
    assert r.code == 2
    error = r.envelope["error"]
    assert error["code"] == "input_invalid" and error["retryable"] is False
    assert {"path": "inner.n", "message": "int_parsing"} in error["details"]
    assert {"path": "tags.1", "message": "int_parsing"} in error["details"]
    assert "SECRET" not in json.dumps(r.envelope)


def test_input_not_json_is_input_invalid(run_script):
    r = run_script(GREETER, raw_input="{not json")
    assert r.code == 2
    assert r.envelope["error"]["details"] == [{"path": "", "message": "json_invalid"}]


def test_missing_entry_point_writes_no_result(run_script):
    r = run_script("x = 1\n")
    assert r.code == 3 and r.envelope is None
    assert "no entry point" in r.err


def test_wrong_signature_writes_no_result(run_script):
    r = run_script("from mobilerun_sandbox import script\n@script\ndef main(input): pass\n")
    assert r.code == 3 and r.envelope is None and "'input'" in r.err


def test_async_entry_point(run_script):
    r = run_script(
        HEADER
        + """
import asyncio

@script
async def main(input: In, ctx: Context) -> Out:
    await asyncio.sleep(0)
    return Out(greeting="async")
""",
    )
    assert r.code == 0 and r.envelope["result"] == {"greeting": "async"}


def test_none_return(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In) -> None:
    pass
""",
    )
    assert r.code == 0
    assert r.envelope == {"ok": True, "outcome": "succeeded", "result": None}


def test_optional_output_returning_none(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In) -> Out | None:
    return None
""",
    )
    assert r.code == 0 and r.envelope["result"] is None


def test_returning_a_value_without_output_model_fails(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In) -> None:
    return {"a": 1}
""",
    )
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"


def test_returning_wrong_shape_fails(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In) -> Out:
    return {"greeting": 5}
""",
    )
    assert r.code == 1
    error = r.envelope["error"]
    assert error["code"] == "output_invalid"
    assert error["details"] == [{"path": "greeting", "message": "string_type"}]


def test_dict_return_is_validated_into_the_model(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In) -> Out:
    return {"greeting": "from dict"}
""",
    )
    assert r.code == 0 and r.envelope["result"] == {"greeting": "from dict"}


def test_context_members_not_available(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    seen = []
    for name, get in [
        ("device", lambda: ctx.device()),
        ("credentials", lambda: ctx.credentials),
        ("files", lambda: ctx.files),
        ("llm", lambda: ctx.llm),
    ]:
        try:
            get()
        except NotAvailableError as exc:
            assert name in str(exc) and exc.member == name
            seen.append(name)
    return Out(greeting=",".join(seen))
""",
    )
    assert r.code == 0
    assert r.envelope["result"] == {"greeting": "device,credentials,files,llm"}


def test_run_info_from_flags(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    run = ctx.run
    return Out(greeting=f"{run.run_id}|{run.attempt}|{run.mode}|{run.device_id}|{run.deadline}")
""",
        extra_args=[
            "--run-id", "r1", "--attempt", "3", "--mode", "dry_run",
            "--device-id", "d9", "--deadline", "2026-11-10T12:00:00+00:00",
        ],
    )  # fmt: skip
    assert r.envelope["result"] == {"greeting": "r1|3|dry_run|d9|2026-11-10 12:00:00+00:00"}


def test_run_info_defaults(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    run = ctx.run
    return Out(greeting=f"{run.run_id}|{run.attempt}|{run.mode}|{run.device_id}|{run.deadline}")
""",
    )
    assert r.envelope["result"] == {"greeting": "local|1|live|None|None"}


def test_log_goes_to_stderr_and_status_to_file(run_script, tmp_path, monkeypatch):
    status = tmp_path / "status.txt"
    monkeypatch.setenv("MOBILERUN_SANDBOX_STATUS_FILE", str(status))
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    ctx.log.info("one")
    ctx.log.warning("two\\nlines")
    ctx.log.error("three")
    ctx.log.status("step 1")
    ctx.log.status("step 2")
    return Out(greeting="ok")
""",
    )
    assert r.code == 0 and r.out == ""
    assert r.err.splitlines() == ["[INFO] one", "[WARNING] two\\nlines", "[ERROR] three"]
    assert status.read_text() == "step 2\n"


def test_status_without_env_is_a_noop(run_script, monkeypatch):
    monkeypatch.delenv("MOBILERUN_SANDBOX_STATUS_FILE", raising=False)
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> None:
    ctx.log.status("nothing happens")
""",
    )
    assert r.code == 0


# partial outcome


def test_partial_with_result(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    ctx.partial("first")
    ctx.partial("page 2 of 3 failed")
    return Out(greeting="some")
""",
    )
    assert r.code == 0
    assert r.envelope == {
        "ok": True,
        "outcome": "partial",
        "partialReason": "page 2 of 3 failed",
        "result": {"greeting": "some"},
    }


def test_partial_without_result(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> None:
    ctx.partial("nothing usable")
""",
    )
    assert r.envelope == {
        "ok": True,
        "outcome": "partial",
        "partialReason": "nothing usable",
        "result": None,
    }


def test_partial_is_dropped_on_failure(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    ctx.partial("x")
    raise ScriptError("late", "failed after partial")
""",
    )
    assert r.code == 1 and "outcome" not in r.envelope and "partialReason" not in r.envelope


# result size

SIZED = (
    HEADER
    + """
from mobilerun_sandbox import RESULT_MAX_BYTES

@script
def main(input: In) -> Out:
    # compact JSON is {"greeting":"..."}: 15 bytes around the text
    return Out(greeting="x" * (RESULT_MAX_BYTES - 15 + input.extra))
"""
)


def test_result_exactly_at_the_limit(run_script):
    r = run_script(SIZED, {"extra": 0})
    assert r.code == 0
    assert len(json.dumps(r.envelope["result"], separators=(",", ":"))) == RESULT_MAX_BYTES


def test_result_one_byte_over_the_limit(run_script):
    r = run_script(SIZED, {"extra": 1})
    assert r.code == 1
    assert r.envelope == {
        "ok": False,
        "error": {
            "code": "result_too_large",
            "message": f"result is {RESULT_MAX_BYTES + 1} bytes; the limit is {RESULT_MAX_BYTES}",
            "retryable": False,
        },
    }


def test_result_size_counts_utf8_bytes(run_script):
    r = run_script(
        HEADER
        + """
from mobilerun_sandbox import RESULT_MAX_BYTES

@script
def main(input: In) -> Out:
    return Out(greeting="é" * (RESULT_MAX_BYTES // 2))  # 2 bytes each, plus the wrapper
""",
    )
    assert r.code == 1 and r.envelope["error"]["code"] == "result_too_large"


# dataset

DATASET = """
from pydantic import BaseModel
from mobilerun_sandbox import Context, Dataset, script

class In(BaseModel):
    pass

class Item(BaseModel):
    id: str
    price: float

@script
def main(input: In, ctx: Context) -> Dataset[Item]:
    return Dataset(items=[Item(id="a", price=1.5), {"id": "b", "price": 2}], complete=False)
"""


def test_dataset_round_trip(run_script, capsys):
    r = run_script(DATASET)
    assert r.code == 0
    assert r.envelope["result"] == {
        "items": [{"id": "a", "price": 1.5}, {"id": "b", "price": 2.0}],
        "complete": False,
    }


def test_dataset_complete_defaults_to_true(run_script):
    r = run_script(DATASET.replace(", complete=False", ""))
    assert r.envelope["result"]["complete"] is True


# state

STATE_SCRIPT = (
    HEADER
    + """
@script
def main(input: In, ctx: Context) -> Out:
    prev = ctx.state.previous
    ctx.log.info(f"prev={prev!r} version={ctx.state.previous_version}")
    if input.name == "save":
        ctx.state.save({"seen": ["a", "b"], "n": input.count}, schema=2)
    if input.name == "save_default":
        ctx.state.save({"n": 1})
    if input.name == "save_twice":
        ctx.state.save({"n": 1})
        ctx.state.save({"n": 2})
    return Out(greeting="ok")
"""
)


def test_state_flag_absent(run_script):
    r = run_script(STATE_SCRIPT)
    assert r.code == 0 and "prev=None version=None" in r.err and "state" not in r.envelope


def test_state_save_without_flag_raises(run_script):
    r = run_script(STATE_SCRIPT, {"name": "save"})
    assert r.code == 1
    assert r.envelope["error"]["message"].startswith("NotAvailableError: ctx.state.save")


def test_state_null(run_script):
    r = run_script(STATE_SCRIPT, state=None)
    assert r.code == 0 and "prev=None version=None" in r.err and "state" not in r.envelope


def test_state_null_then_save(run_script):
    r = run_script(STATE_SCRIPT, {"name": "save", "count": 5}, state=None)
    assert r.code == 0
    assert r.envelope["state"] == {"meta": {"schema": 2}, "data": {"seen": ["a", "b"], "n": 5}}


def test_state_previous_snapshot(run_script):
    previous = {"version": 7, "meta": {"schema": 1}, "data": {"n": 41}}
    r = run_script(STATE_SCRIPT, state=previous)
    assert r.code == 0 and "prev={'n': 41} version=7" in r.err
    assert "state" not in r.envelope  # nothing saved, nothing returned


def test_state_save_default_schema_and_last_call_wins(run_script):
    r = run_script(STATE_SCRIPT, {"name": "save_default"}, state=None)
    assert r.envelope["state"] == {"meta": {"schema": 1}, "data": {"n": 1}}
    r = run_script(STATE_SCRIPT, {"name": "save_twice"}, state=None)
    assert r.envelope["state"]["data"] == {"n": 2}


def test_state_is_dropped_on_failure(run_script):
    r = run_script(
        HEADER
        + """
@script
def main(input: In, ctx: Context) -> Out:
    ctx.state.save({"n": 1})
    raise ScriptError("late", "after save")
""",
        state=None,
    )
    assert r.code == 1 and "state" not in r.envelope


def _state_size_script(extra: int) -> str:
    return (
        HEADER
        + f"""
from mobilerun_sandbox import STATE_MAX_BYTES

@script
def main(input: In, ctx: Context) -> None:
    ctx.state.save("x" * (STATE_MAX_BYTES - 2 + {extra}))  # JSON adds two quotes
"""
    )


def test_state_exactly_at_the_limit(run_script):
    r = run_script(_state_size_script(0), state=None)
    assert r.code == 0 and len(r.envelope["state"]["data"]) == STATE_MAX_BYTES - 2


def test_state_oversize_fails_the_run(run_script):
    r = run_script(_state_size_script(1), state=None)
    assert r.code == 1
    assert r.envelope == {
        "ok": False,
        "error": {
            "code": "state_too_large",
            "message": (f"state is {STATE_MAX_BYTES + 1} bytes; the limit is {STATE_MAX_BYTES}"),
            "retryable": False,
        },
    }


def test_state_file_with_wrong_shape_is_a_runner_fault(run_script):
    r = run_script(STATE_SCRIPT, state={"version": "1"})
    assert r.code == 64 and r.envelope is None and "state file" in r.err


# runner misuse


def test_usage_error_does_not_use_exit_code_2(capsys):
    import pytest

    from mobilerun_sandbox import cli

    with pytest.raises(SystemExit) as info:
        cli.main(["run", "script.py"])
    assert info.value.code == 64
    assert "required" in capsys.readouterr().err


def test_missing_input_file_is_a_runner_fault(write_script, tmp_path):
    from mobilerun_sandbox import cli

    path = write_script(GREETER)
    code = cli.main(
        ["run", str(path), "--input-file", str(tmp_path / "none.json"),
         "--result-file", str(tmp_path / "out.json")]
    )  # fmt: skip
    assert code == 64 and not (tmp_path / "out.json").exists()


def test_result_file_is_replaced_atomically(run_script, tmp_path):
    r = run_script(GREETER)
    assert r.code == 0
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("out")) == ["out.json"]


# real process: the command lines other services call


def test_module_invocation_in_a_subprocess(write_script, tmp_path):
    script = write_script(GREETER)
    inp, out = tmp_path / "in.json", tmp_path / "out.json"
    inp.write_text('{"name": "proc"}')
    base = [sys.executable, "-m", "mobilerun_sandbox"]
    inspected = subprocess.run([*base, "inspect", str(script)], capture_output=True, text=True)
    assert inspected.returncode == 0
    assert json.loads(inspected.stdout)["entrypoint"] == "main"
    ran = subprocess.run(
        [*base, "run", str(script), "--input-file", str(inp), "--result-file", str(out)],
        capture_output=True,
        text=True,
    )
    assert ran.returncode == 0
    assert json.loads(out.read_text())["result"] == {"greeting": "hi proc x1"}
    assert not (tmp_path / "__pycache__").exists()


def test_public_api_names():
    import mobilerun_sandbox

    assert sorted(mobilerun_sandbox.__all__) == sorted(
        [
            "Context", "Dataset", "EntryPointError", "Log", "NotAvailableError",
            "RESULT_MAX_BYTES", "RunInfo", "STATE_MAX_BYTES", "ScriptError", "script",
        ]
    )  # fmt: skip
    assert RESULT_MAX_BYTES == 524288 and STATE_MAX_BYTES == 524288


def test_sdk_version_matches_pyproject():
    import tomllib
    from pathlib import Path

    from mobilerun_sandbox._version import SDK_VERSION

    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert data["project"]["version"] == SDK_VERSION == "0.1.0"
