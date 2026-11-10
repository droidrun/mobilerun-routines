"""B4 (one incomplete signal), B5 (SIGTERM) and D1 (incremental dataset output)."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from script_source import script_source

from mobilerun_sandbox import DATASET_MAX_BYTES, Dataset

MODELS = """
from mobilerun_sandbox import DATASET_MAX_BYTES, DatasetTooLargeError, key_field

class In(BaseModel):
    n: int = 2
    pad: int = 0
    ready: str = ""

class Out(BaseModel):
    greeting: str = ""

class Item(BaseModel):
    id: str = key_field()
    price: float = 0.0

class Aliased(BaseModel):
    id: str = key_field()
    unit_price: float = Field(default=0.0, serialization_alias="unitPrice")
"""


def ds_script(body: str, ret: str | None = "Dataset[Item] | None", **kwargs) -> str:
    return script_source(body, ret=ret, models=MODELS, **kwargs)


@pytest.fixture
def dataset_file(tmp_path: Path) -> Path:
    return tmp_path / "dataset.jsonl"


def lines(path: Path) -> list[dict]:
    text = path.read_text()
    assert text == "" or text.endswith("\n")  # only complete lines
    return [json.loads(line) for line in text.splitlines()]


PUSH_TWO = 'ctx.dataset.push(Item(id="a", price=1.5))\nctx.dataset.push({"id": "b", "price": 2})'

# B4


def test_dataset_has_only_items():
    assert Dataset[int](items=[1]).items == [1]
    with pytest.raises(ValidationError):
        Dataset[int](items=[1], complete=False)
    assert "complete" not in Dataset[int].model_fields


def test_returning_complete_in_a_dict_is_output_invalid(run_script):
    r = run_script(ds_script('return {"items": [], "complete": False}', ret="Dataset[Item]"))
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"


def test_incomplete_list_is_reported_with_partial(run_script):
    r = run_script(ds_script('ctx.partial("page 2 failed")\nreturn Dataset(items=[Item(id="a")])'))
    assert r.code == 0
    assert r.envelope["outcome"] == "partial"
    assert r.envelope["partialReason"] == "page 2 failed"
    assert r.envelope["result"] == {"items": [{"id": "a", "price": 0.0}]}


# D1: push to the dataset file


def test_push_writes_one_json_line_per_item(run_script, dataset_file):
    r = run_script(ds_script(PUSH_TWO), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 0, r.envelope
    assert lines(dataset_file) == [{"id": "a", "price": 1.5}, {"id": "b", "price": 2.0}]
    assert r.envelope == {
        "ok": True,
        "outcome": "succeeded",
        "result": None,
        "dataset": {"items": 2, "bytes": dataset_file.stat().st_size},
    }


def test_pushed_lines_use_serialization_aliases(run_script, dataset_file):
    body = 'ctx.dataset.push(Aliased(id="a", unit_price=3))\nctx.dataset.push({"id": "b"})'
    r = run_script(
        ds_script(body, ret="Dataset[Aliased] | None"),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 0, r.envelope
    assert lines(dataset_file) == [{"id": "a", "unitPrice": 3.0}, {"id": "b", "unitPrice": 0.0}]


def test_pushed_item_built_without_validation_is_checked(run_script, dataset_file):
    body = 'ctx.dataset.push(Item(id="ok"))\nctx.dataset.push(Item.model_construct(id=5))'
    r = run_script(ds_script(body), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 1
    error = r.envelope["error"]
    assert error["code"] == "output_invalid"
    assert error["details"] == [{"path": "id", "message": "string_type"}]
    assert lines(dataset_file) == [{"id": "ok", "price": 0.0}]  # lines before stay
    assert r.envelope["dataset"]["items"] == 1


def test_pushed_invalid_dict_does_not_leak_values(run_script, dataset_file):
    r = run_script(
        ds_script('ctx.dataset.push({"id": "SECRET", "price": "nope"})'),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"
    assert "SECRET" not in json.dumps(r.envelope) and "nope" not in json.dumps(r.envelope)


def test_push_without_the_flag_is_not_available(run_script):
    r = run_script(ds_script(PUSH_TWO))
    assert r.code == 1
    assert r.envelope["error"]["message"].startswith("NotAvailableError: ctx.dataset.push")
    assert "dataset" not in r.envelope


def test_push_needs_a_dataset_output(run_script, dataset_file):
    r = run_script(
        ds_script('ctx.dataset.push({"id": "a"})', ret="Out"),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 1
    assert r.envelope["error"]["message"].startswith("NotAvailableError: ctx.dataset.push")
    assert r.envelope["dataset"] == {"items": 0, "bytes": 0}


def test_push_needs_a_dataset_output_without_any_output(run_script, dataset_file):
    r = run_script(
        ds_script('ctx.dataset.push({"id": "a"})', ret=None),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 1
    assert r.envelope["error"]["message"].startswith("NotAvailableError: ctx.dataset.push")


@pytest.mark.parametrize("ret", ["Dataset[Item]", "Dataset[Item] | None"])
def test_entry_point_may_return_none_after_push(run_script, dataset_file, ret):
    r = run_script(ds_script(PUSH_TWO, ret=ret), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 0 and r.envelope["result"] is None


def test_returned_dataset_is_appended_after_pushed_items(run_script, dataset_file):
    body = 'ctx.dataset.push(Item(id="a"))\nreturn Dataset(items=[Item(id="b"), {"id": "c"}])'
    r = run_script(ds_script(body), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 0, r.envelope
    assert [row["id"] for row in lines(dataset_file)] == ["a", "b", "c"]
    assert r.envelope["result"] is None
    assert r.envelope["dataset"] == {"items": 3, "bytes": dataset_file.stat().st_size}


def test_returned_invalid_dataset_writes_nothing(run_script, dataset_file):
    r = run_script(
        ds_script('return {"items": [{"id": 5}]}'),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 1 and r.envelope["error"]["code"] == "output_invalid"
    assert lines(dataset_file) == []


def test_returned_dataset_without_the_flag_is_unchanged(run_script):
    r = run_script(ds_script('return Dataset(items=[Item(id="a")])'))
    assert r.code == 0
    assert r.envelope["result"] == {"items": [{"id": "a", "price": 0.0}]}
    assert "dataset" not in r.envelope


def test_the_file_is_truncated_at_start(run_script, dataset_file):
    dataset_file.write_text('{"old":1}\n{"old":2}\n')
    r = run_script(ds_script("pass", ret=None), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 0
    assert dataset_file.read_text() == ""
    assert r.envelope["dataset"] == {"items": 0, "bytes": 0}


def test_every_envelope_carries_the_dataset_counts(run_script, dataset_file):
    extra = ["--dataset-file", str(dataset_file)]
    failed = run_script(
        ds_script('ctx.dataset.push({"id": "a"})\nraise ScriptError("late", "no")'),
        extra_args=extra,
    )
    assert failed.code == 1 and failed.envelope["dataset"]["items"] == 1

    partial = run_script(
        ds_script('ctx.dataset.push({"id": "a"})\nctx.partial("some")'), extra_args=extra
    )
    assert partial.envelope["outcome"] == "partial" and partial.envelope["dataset"]["items"] == 1

    invalid = run_script(ds_script("pass"), {"n": "x"}, extra_args=extra)
    assert invalid.code == 2 and invalid.envelope["dataset"] == {"items": 0, "bytes": 0}

    crashed = run_script("raise RuntimeError('import')\n", extra_args=extra)
    assert crashed.code == 1 and crashed.envelope["dataset"] == {"items": 0, "bytes": 0}


def test_unwritable_dataset_file_is_a_runner_fault(run_script, tmp_path):
    r = run_script(
        ds_script("pass"), extra_args=["--dataset-file", str(tmp_path / "missing-dir" / "d.jsonl")]
    )
    assert r.code == 64 and r.envelope is None


# D1: size cap

def _line_bytes(id_len: int) -> int:
    return len(json.dumps({"id": "x" * id_len, "price": 0.0}, separators=(",", ":"))) + 1


def test_dataset_max_bytes_value():
    assert DATASET_MAX_BYTES == 524288


def test_a_line_exactly_at_the_limit_fits(run_script, dataset_file):
    id_len = DATASET_MAX_BYTES - _line_bytes(0)
    r = run_script(
        ds_script(f'ctx.dataset.push({{"id": "x" * {id_len}}})'),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 0, r.envelope
    assert dataset_file.stat().st_size == DATASET_MAX_BYTES
    assert r.envelope["dataset"] == {"items": 1, "bytes": DATASET_MAX_BYTES}


def test_a_line_one_byte_over_the_limit_fails_with_dataset_too_large(run_script, dataset_file):
    id_len = DATASET_MAX_BYTES - _line_bytes(0) + 1
    r = run_script(
        ds_script(f'ctx.dataset.push({{"id": "x" * {id_len}}})'),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 1
    assert r.envelope["error"]["code"] == "dataset_too_large"
    assert r.envelope["error"]["retryable"] is False
    assert dataset_file.read_text() == ""
    assert r.envelope["dataset"] == {"items": 0, "bytes": 0}


def test_pushing_past_the_limit_keeps_the_lines_written_before(run_script, dataset_file):
    body = 'for i in range(10_000):\n    ctx.dataset.push({"id": f"{i:06}" + "x" * 1000})'
    r = run_script(ds_script(body), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 1 and r.envelope["error"]["code"] == "dataset_too_large"
    rows = lines(dataset_file)
    assert 0 < len(rows) < 10_000
    assert dataset_file.stat().st_size <= DATASET_MAX_BYTES
    assert r.envelope["dataset"] == {"items": len(rows), "bytes": dataset_file.stat().st_size}


def test_a_script_can_catch_the_limit_and_report_partial(run_script, dataset_file):
    body = """
    try:
        for i in range(10_000):
            ctx.dataset.push({"id": f"{i:06}" + "x" * 1000})
    except DatasetTooLargeError:
        ctx.partial("dataset full")
    """
    r = run_script(ds_script(body), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 0 and r.envelope["outcome"] == "partial"
    assert r.envelope["partialReason"] == "dataset full"


def test_a_returned_dataset_over_the_limit_fails(run_script, dataset_file):
    body = 'return Dataset(items=[{"id": f"{i:06}" + "x" * 1000} for i in range(10_000)])'
    r = run_script(ds_script(body), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 1 and r.envelope["error"]["code"] == "dataset_too_large"
    assert dataset_file.stat().st_size <= DATASET_MAX_BYTES
    lines(dataset_file)


def test_the_result_limit_is_not_used_with_a_dataset_file(run_script, dataset_file):
    # 600 KiB in total is far over RESULT_MAX_BYTES but each run only writes DATASET_MAX_BYTES
    body = 'return Dataset(items=[{"id": f"{i:03}" + "x" * 1000} for i in range(400)])'
    r = run_script(ds_script(body), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 0 and len(lines(dataset_file)) == 400


# B5: SIGTERM leaves a result


def _spawn(script: Path, tmp_path: Path, *, dataset: bool) -> tuple[subprocess.Popen, Path, Path]:
    ready, result = tmp_path / "ready", tmp_path / "out.json"
    (tmp_path / "in.json").write_text(json.dumps({"ready": str(ready)}))
    argv = [
        sys.executable, "-m", "mobilerun_sandbox", "run", str(script),
        "--input-file", str(tmp_path / "in.json"), "--result-file", str(result),
    ]  # fmt: skip
    dataset_path = tmp_path / "dataset.jsonl"
    if dataset:
        argv += ["--dataset-file", str(dataset_path)]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc, ready, dataset_path


def _terminate(proc: subprocess.Popen, ready: Path) -> int:
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "script never became ready"
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        return proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


CANCELLED = {
    "ok": False,
    "error": {"code": "cancelled", "message": "terminated by signal", "retryable": False},
}
SLEEP_AFTER_PUSH = """
{wait_import}
ctx.dataset.push({{"id": "a"}})
ctx.dataset.push({{"id": "b", "price": 2}})
open(input.ready, "w").close()
{sleep}
"""


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_sigterm_leaves_a_cancelled_envelope_and_the_pushed_lines(write_script, tmp_path, is_async):
    sleep = "await asyncio.sleep(60)" if is_async else "time.sleep(60)"
    source = ds_script(
        SLEEP_AFTER_PUSH.format(wait_import="import asyncio, time", sleep=sleep),
        is_async=is_async,
    )
    proc, ready, dataset_path = _spawn(write_script(source), tmp_path, dataset=True)
    code = _terminate(proc, ready)
    assert code == 1
    envelope = json.loads((tmp_path / "out.json").read_text())
    assert envelope == {**CANCELLED, "dataset": {"items": 2, "bytes": dataset_path.stat().st_size}}
    assert lines(dataset_path) == [{"id": "a", "price": 0.0}, {"id": "b", "price": 2.0}]


def test_sigterm_without_a_dataset_file_has_no_dataset_counts(write_script, tmp_path):
    body = 'import time\nopen(input.ready, "w").close()\ntime.sleep(60)'
    proc, ready, _ = _spawn(write_script(ds_script(body, ret="Out")), tmp_path, dataset=False)
    assert _terminate(proc, ready) == 1
    assert json.loads((tmp_path / "out.json").read_text()) == CANCELLED


def test_sigterm_in_a_busy_loop(write_script, tmp_path):
    body = 'open(input.ready, "w").close()\nwhile True:\n    sum(range(1000))'
    proc, ready, _ = _spawn(write_script(ds_script(body, ret="Out")), tmp_path, dataset=False)
    assert _terminate(proc, ready) == 1
    assert json.loads((tmp_path / "out.json").read_text()) == CANCELLED


def test_the_sigterm_handler_is_restored_after_an_in_process_run(run_script):
    before = signal.getsignal(signal.SIGTERM)
    r = run_script(ds_script("pass", ret=None))
    assert r.code == 0
    assert signal.getsignal(signal.SIGTERM) is before
