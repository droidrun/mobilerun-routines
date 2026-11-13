import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from script_source import GREETER, script_source

from mobilerun_routines import (
    ERROR_MESSAGE_MAX_CHARS,
    RESULT_MAX_BYTES,
    STATE_MAX_BYTES,
    cli,
)
from mobilerun_routines._io import atomic_write_text
from mobilerun_routines._version import SDK_VERSION


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
        script_source('raise ScriptError("login_failed", "bad password", retryable=True)')
    )
    assert r.code == 1
    assert r.envelope == {
        "ok": False,
        "error": {"code": "login_failed", "message": "bad password", "retryable": True},
    }


def test_script_error_defaults_to_not_retryable(run_script):
    r = run_script(script_source('raise ScriptError("nope", "no")', sig="input: In"))
    assert r.envelope["error"]["retryable"] is False


def test_unexpected_exception(run_script):
    r = run_script(script_source('raise ValueError("boom " + "x" * 600)'))
    assert r.code == 1
    error = r.envelope["error"]
    assert r.envelope["ok"] is False
    assert error["code"] == "script_error" and error["retryable"] is False
    assert error["message"].startswith("ValueError: boom x")
    assert len(error["message"]) == ERROR_MESSAGE_MAX_CHARS  # the cap covers the whole message
    assert "Traceback" in r.err


def test_sys_exit_in_entry_point_is_a_script_error(run_script):
    r = run_script(script_source("import sys\nsys.exit(3)"))
    assert r.code == 1 and r.envelope["error"]["message"] == "SystemExit: 3"


# exceptions outside the entry point body still end in an envelope

THROWING_VALIDATOR = """
class In(BaseModel):
    name: str = "x"

    @field_validator("name")
    @classmethod
    def check(cls, value):
        raise TypeError("validator blew up")

class Out(BaseModel):
    greeting: str = ""
"""

THROWING_SERIALIZER = """
class In(BaseModel):
    pass

class Out(BaseModel):
    greeting: str

    @field_serializer("greeting")
    def ser(self, value):
        raise RuntimeError("serializer blew up")
"""


def test_input_validator_raising_a_non_validation_error(run_script):
    r = run_script(script_source(models=THROWING_VALIDATOR), {"name": "a"})
    assert r.code == 1
    assert r.envelope["error"]["code"] == "script_error"
    assert r.envelope["error"]["message"] == "TypeError: validator blew up"
    assert "Traceback" in r.err


def test_output_serializer_raising(run_script):
    r = run_script(script_source('return Out(greeting="a")', models=THROWING_SERIALIZER))
    assert r.code == 1
    assert r.envelope["error"]["code"] == "script_error"
    assert "serializer blew up" in r.envelope["error"]["message"]
    assert "Traceback" in r.err


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("raise RuntimeError('at import')\n", "RuntimeError: at import"),
        ("import sys\nsys.exit(0)\n", "SystemExit: 0"),
        ("open('missing-resource')\n", "FileNotFoundError: [Errno 2]"),
        ("def broken(:\n", "SyntaxError"),
    ],
    ids=["exception", "sys-exit-0", "open-missing-file", "syntax-error"],
)
def test_import_time_failure_is_a_script_error(run_script, source, message):
    r = run_script(source)
    assert r.code == 1
    assert r.envelope["error"]["code"] == "script_error"
    assert r.envelope["error"]["message"].startswith(message)


def test_unreadable_script_is_a_runner_fault(run_script, tmp_path):
    code = cli.main(
        ["run", str(tmp_path / "none.py"), "--input-file", str(tmp_path / "i.json"),
         "--result-file", str(tmp_path / "out.json")]
    )  # fmt: skip
    assert code == 64 and not (tmp_path / "out.json").exists()


# input and output validation

NESTED = """
class Inner(BaseModel):
    n: int

class In(BaseModel):
    inner: Inner
    tags: list[int]
    entries: dict[str, int] = {}

class Out(BaseModel):
    greeting: str
"""


def test_invalid_input_reports_paths_not_values(run_script):
    r = run_script(
        script_source(sig="input: In", ret="None", models=NESTED),
        {"inner": {"n": "SECRET-VALUE"}, "tags": [1, "SECRET-TWO"]},
    )
    assert r.code == 2
    error = r.envelope["error"]
    assert error["code"] == "input_invalid" and error["retryable"] is False
    assert {"path": "inner.n", "message": "int_parsing"} in error["details"]
    assert {"path": "tags.1", "message": "int_parsing"} in error["details"]
    assert "SECRET" not in json.dumps(r.envelope)


def test_input_derived_dictionary_keys_are_not_leaked(run_script):
    r = run_script(
        script_source(sig="input: In", ret="None", models=NESTED),
        {"inner": {"n": 1}, "tags": [], "entries": {"SECRET-TOKEN": "bad"}},
    )
    assert r.code == 2
    assert r.envelope["error"]["details"] == [{"path": "entries.*", "message": "int_parsing"}]
    assert "SECRET" not in json.dumps(r.envelope)


def test_input_not_json_is_input_invalid(run_script):
    r = run_script(GREETER, raw_input="{not json")
    assert r.code == 2
    assert r.envelope["error"]["details"] == [{"path": "", "message": "json_invalid"}]


def test_returning_wrong_shape_fails(run_script):
    r = run_script(script_source('return {"greeting": 5}'))
    assert r.code == 1
    error = r.envelope["error"]
    assert error["code"] == "output_invalid"
    assert error["details"] == [{"path": "greeting", "message": "string_type"}]


def test_model_construct_with_wrong_type_is_validated(run_script):
    r = run_script(script_source("return Out.model_construct(greeting=123)"))
    assert r.code == 1
    assert r.envelope["error"]["code"] == "output_invalid"
    assert r.envelope["error"]["details"] == [{"path": "greeting", "message": "string_type"}]


def test_mutated_output_instance_is_validated(run_script):
    r = run_script(script_source('out = Out(greeting="ok")\nout.greeting = 5\nreturn out'))
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"


def test_output_dictionary_keys_are_not_leaked(run_script):
    models = """
    class In(BaseModel):
        pass

    class Out(BaseModel):
        scores: dict[str, int]
    """
    r = run_script(
        script_source('return Out.model_construct(scores={"SECRET-KEY": "bad"})', models=models)
    )
    assert r.code == 1
    assert r.envelope["error"]["details"] == [{"path": "scores.*", "message": "int_parsing"}]
    assert "SECRET" not in json.dumps(r.envelope)


def test_dict_return_is_validated_into_the_model(run_script):
    r = run_script(script_source('return {"greeting": "from dict"}'))
    assert r.code == 0 and r.envelope["result"] == {"greeting": "from dict"}


def test_returning_a_value_without_output_model_fails(run_script):
    r = run_script(script_source('return {"a": 1}', sig="input: In", ret="None"))
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"


# entry point errors, return shapes


def test_missing_entry_point_writes_no_result(run_script):
    r = run_script("x = 1\n")
    assert r.code == 3 and r.envelope is None
    assert "no entry point" in r.err


def test_wrong_signature_writes_no_result(run_script):
    r = run_script(script_source(sig="input"))
    assert r.code == 3 and r.envelope is None and "'input'" in r.err


def test_async_entry_point(run_script):
    r = run_script(
        script_source(
            'await asyncio.sleep(0)\nreturn Out(greeting="async")',
            before="import asyncio",
            is_async=True,
        )
    )
    assert r.code == 0 and r.envelope["result"] == {"greeting": "async"}


def test_none_return(run_script):
    r = run_script(script_source(sig="input: In", ret="None"))
    assert r.code == 0
    assert r.envelope == {"ok": True, "outcome": "succeeded", "result": None}


def test_optional_output_returning_none(run_script):
    r = run_script(script_source("return None", sig="input: In", ret="Out | None"))
    assert r.code == 0 and r.envelope["result"] is None


# context

CONTEXT_MEMBERS = """
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
"""

RUN_INFO = (
    "run = ctx.run\n"
    'return Out(greeting=f"{run.run_id}|{run.attempt}|{run.mode}|{run.device_id}|{run.deadline}")'
)


def test_context_members_not_available(run_script):
    r = run_script(script_source(CONTEXT_MEMBERS))
    assert r.code == 0
    assert r.envelope["result"] == {"greeting": "device,credentials,files,llm"}


def test_run_info_from_flags(run_script):
    r = run_script(
        script_source(RUN_INFO),
        extra_args=[
            "--run-id", "r1", "--attempt", "3", "--mode", "dry_run",
            "--device-id", "d9", "--deadline", "2026-11-10T12:00:00+00:00",
        ],
    )  # fmt: skip
    assert r.envelope["result"] == {"greeting": "r1|3|dry_run|d9|2026-11-10 12:00:00+00:00"}


def test_run_info_defaults(run_script):
    r = run_script(script_source(RUN_INFO))
    assert r.envelope["result"] == {"greeting": "local|1|live|None|None"}


def test_log_goes_to_stderr_and_status_to_file(run_script, tmp_path, monkeypatch):
    status = tmp_path / "status.txt"
    monkeypatch.setenv("MOBILERUN_SANDBOX_STATUS_FILE", str(status))
    body = """
    ctx.log.info("one")
    ctx.log.warning("two\\nlines")
    ctx.log.error("three")
    ctx.log.status("step 1")
    ctx.log.status("step 2")
    return Out(greeting="ok")
    """
    r = run_script(script_source(body))
    assert r.code == 0 and r.out == ""
    assert r.err.splitlines() == ["[INFO] one", "[WARNING] two\\nlines", "[ERROR] three"]
    assert status.read_text() == "step 2\n"


def test_status_without_env_is_a_noop(run_script, monkeypatch):
    monkeypatch.delenv("MOBILERUN_SANDBOX_STATUS_FILE", raising=False)
    r = run_script(script_source('ctx.log.status("nothing happens")', ret="None"))
    assert r.code == 0


# partial outcome


def test_partial_with_result(run_script):
    r = run_script(
        script_source(
            'ctx.partial("first")\nctx.partial("page 2 of 3 failed")\nreturn Out(greeting="some")'
        )
    )
    assert r.code == 0
    assert r.envelope == {
        "ok": True,
        "outcome": "partial",
        "partialReason": "page 2 of 3 failed",
        "result": {"greeting": "some"},
    }


def test_partial_without_result(run_script):
    r = run_script(script_source('ctx.partial("nothing usable")', ret="None"))
    assert r.envelope == {
        "ok": True,
        "outcome": "partial",
        "partialReason": "nothing usable",
        "result": None,
    }


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("x" * 500, "x" * 500), ("x" * 501, "x" * 500), ("", "")],
    ids=["at-cap", "over-cap", "empty"],
)
def test_partial_reason_cap_boundaries(run_script, reason, expected):
    r = run_script(script_source(f"ctx.partial({reason!r})", ret="None"))
    assert r.code == 0
    assert r.envelope["outcome"] == "partial" and r.envelope["partialReason"] == expected


def test_partial_is_dropped_on_failure(run_script):
    r = run_script(script_source('ctx.partial("x")\nraise ScriptError("late", "failed")'))
    assert r.code == 1 and "outcome" not in r.envelope and "partialReason" not in r.envelope


# result size

SIZED = script_source(
    # compact JSON is {"greeting":"..."}: 15 bytes around the text
    'return Out(greeting="x" * (RESULT_MAX_BYTES - 15 + input.extra))',
    sig="input: In",
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
    r = run_script(script_source('return Out(greeting="é" * (RESULT_MAX_BYTES // 2))'))
    assert r.code == 1 and r.envelope["error"]["code"] == "result_too_large"


# dataset

DATASET = script_source(
    'return Dataset(items=[Item(id="a", price=1.5), {"id": "b", "price": 2}])',
    ret="Dataset[Item]",
    before="""
    class Item(BaseModel):
        id: str
        price: float
    """,
)


def test_dataset_round_trip(run_script):
    r = run_script(DATASET)
    assert r.code == 0
    assert r.envelope["result"] == {
        "items": [{"id": "a", "price": 1.5}, {"id": "b", "price": 2.0}],
    }
    assert "dataset" not in r.envelope  # only with --dataset-file


# state

STATE_BODY = """
ctx.log.info(f"prev={ctx.state.previous!r} schema={ctx.state.previous_schema}")
if input.name == "save":
    ctx.state.save({"seen": ["a", "b"], "n": input.count}, schema=2)
if input.name == "save_default":
    ctx.state.save({"n": 1})
if input.name == "save_twice":
    ctx.state.save({"n": 1})
    ctx.state.save({"n": 2})
return Out(greeting="ok")
"""
STATE_SCRIPT = script_source(STATE_BODY)


def test_state_flag_absent(run_script):
    r = run_script(STATE_SCRIPT)
    assert r.code == 0 and "prev=None schema=None" in r.err and "state" not in r.envelope


def test_state_save_without_flag_raises(run_script):
    r = run_script(STATE_SCRIPT, {"name": "save"})
    assert r.code == 1
    assert r.envelope["error"]["message"].startswith("NotAvailableError: ctx.state.save")


def test_state_null(run_script):
    r = run_script(STATE_SCRIPT, state=None)
    assert r.code == 0 and "prev=None schema=None" in r.err and "state" not in r.envelope


def test_state_null_then_save(run_script):
    r = run_script(STATE_SCRIPT, {"name": "save", "count": 5}, state=None)
    assert r.code == 0
    assert r.envelope["state"] == {"meta": {"schema": 2}, "data": {"seen": ["a", "b"], "n": 5}}


def test_state_previous_snapshot(run_script):
    previous = {"version": 7, "meta": {"schema": 3}, "data": {"n": 41}}
    r = run_script(STATE_SCRIPT, state=previous)
    assert r.code == 0 and "prev={'n': 41} schema=3" in r.err
    assert "state" not in r.envelope  # nothing saved, nothing returned


def test_state_save_default_schema_and_last_call_wins(run_script):
    r = run_script(STATE_SCRIPT, {"name": "save_default"}, state=None)
    assert r.envelope["state"] == {"meta": {"schema": 1}, "data": {"n": 1}}
    r = run_script(STATE_SCRIPT, {"name": "save_twice"}, state=None)
    assert r.envelope["state"]["data"] == {"n": 2}


def test_state_is_dropped_on_failure(run_script):
    r = run_script(
        script_source('ctx.state.save({"n": 1})\nraise ScriptError("late", "after save")'),
        state=None,
    )
    assert r.code == 1 and "state" not in r.envelope


def _state_size_script(extra: int) -> str:
    # the JSON text adds two quotes around the string
    return script_source(f'ctx.state.save("x" * (STATE_MAX_BYTES - 2 + {extra}))', ret="None")


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
            "message": f"state is {STATE_MAX_BYTES + 1} bytes; the limit is {STATE_MAX_BYTES}",
            "retryable": False,
        },
    }


@pytest.mark.parametrize(
    "state", [{"version": "1"}, {"version": True, "meta": {"schema": 1}, "data": 1}]
)
def test_state_file_with_wrong_shape_is_a_runner_fault(run_script, state):
    r = run_script(STATE_SCRIPT, state=state)
    assert r.code == 64 and r.envelope is None and "state file" in r.err


# runner misuse


def test_usage_error_does_not_use_exit_code_2(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["run", "script.py"])
    assert info.value.code == 64
    assert "required" in capsys.readouterr().err


def test_missing_input_file_is_a_runner_fault(write_script, tmp_path):
    path = write_script(GREETER)
    code = cli.main(
        ["run", str(path), "--input-file", str(tmp_path / "none.json"),
         "--result-file", str(tmp_path / "out.json")]
    )  # fmt: skip
    assert code == 64 and not (tmp_path / "out.json").exists()


def test_result_file_goes_through_an_unpredictable_temp_file(run_script, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (tmp_path / "out.json.tmp").symlink_to(victim)  # the old predictable temp name
    r = run_script(GREETER)
    assert r.code == 0 and victim.read_text() == "keep"
    assert (tmp_path / "out.json.tmp").is_symlink()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp-")] == []


def test_atomic_write_leaves_no_temp_file_on_failure(tmp_path):
    with pytest.raises(OSError):
        atomic_write_text(tmp_path / "missing-dir" / "out.json", "x")
    target = tmp_path / "out.json"
    target.mkdir()  # os.replace of a file onto a directory fails
    with pytest.raises(OSError):
        atomic_write_text(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


# real process: the command lines other services call


def test_module_invocation_in_a_subprocess(write_script, tmp_path):
    script = write_script(GREETER)
    inp, out = tmp_path / "in.json", tmp_path / "out.json"
    inp.write_text('{"name": "proc"}')
    base = [sys.executable, "-m", "mobilerun_routines"]
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


def test_sdk_version_matches_pyproject():
    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert data["project"]["version"] == SDK_VERSION
