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

from mobilerun_routines import DATASET_MAX_BYTES, Dataset

MODELS = """
from mobilerun_routines import DATASET_MAX_BYTES, DatasetTooLargeError, key_field

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
    with pytest.raises(ValidationError):
        Dataset[int](items=[1], complete=False)


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


@pytest.mark.parametrize("ret", ["Out", None], ids=["value", "none"])
def test_push_needs_a_dataset_output(run_script, dataset_file, ret):
    r = run_script(
        ds_script('ctx.dataset.push({"id": "a"})', ret=ret),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 1
    assert r.envelope["error"]["message"].startswith("NotAvailableError: ctx.dataset.push")
    assert r.envelope["dataset"] == {"items": 0, "bytes": 0}


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


def test_a_returned_dataset_at_the_limit_fits_and_one_byte_over_fails(run_script, dataset_file):
    # one item whose line (with the newline) is exactly DATASET_MAX_BYTES, or one byte more
    extra = ["--dataset-file", str(dataset_file)]
    id_len = DATASET_MAX_BYTES - _line_bytes(0)
    fits = run_script(
        ds_script(f'return Dataset(items=[{{"id": "x" * {id_len}}}])'), extra_args=extra
    )
    assert fits.code == 0, fits.envelope
    assert fits.envelope["result"] is None
    assert fits.envelope["dataset"] == {"items": 1, "bytes": DATASET_MAX_BYTES}
    over = run_script(
        ds_script(f'return Dataset(items=[{{"id": "x" * {id_len + 1}}}])'), extra_args=extra
    )
    assert over.code == 1 and over.envelope["error"]["code"] == "dataset_too_large"
    assert over.envelope["dataset"] == {"items": 0, "bytes": 0}
    assert dataset_file.read_text() == ""


# B5: SIGTERM leaves a result


def _spawn(script: Path, tmp_path: Path, *, dataset: bool) -> tuple[subprocess.Popen, Path, Path]:
    ready, result = tmp_path / "ready", tmp_path / "out.json"
    (tmp_path / "in.json").write_text(json.dumps({"ready": str(ready)}))
    argv = [
        sys.executable, "-m", "mobilerun_routines", "run", str(script),
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


# Fix round 1: pushed items, interrupted and concurrent appends, cancellation wins

OVERLAP_MODELS = """
from mobilerun_routines import key_field

class In(BaseModel):
    pass

class Item(BaseModel):
    first: int = key_field(alias="second")
    second: int = Field(alias="third")
"""

COMPUTED_MODELS = """
from mobilerun_routines import key_field
from pydantic import ConfigDict, computed_field

class In(BaseModel):
    pass

class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = key_field()

    @computed_field
    @property
    def shout(self) -> str:
        return self.id.upper()
"""


def test_pushed_item_with_overlapping_aliases_keeps_its_values(run_script, dataset_file):
    r = run_script(
        script_source("ctx.dataset.push(Item(second=1, third=2))", ret="Dataset[Item] | None",
                      models=OVERLAP_MODELS),
        extra_args=["--dataset-file", str(dataset_file)],
    )  # fmt: skip
    assert r.code == 0, r.envelope
    assert lines(dataset_file) == [{"second": 1, "third": 2}]


def test_pushed_and_returned_computed_fields_are_written(run_script, dataset_file):
    body = 'ctx.dataset.push(Item(id="a"))\nreturn Dataset(items=[Item(id="b")])'
    r = run_script(
        script_source(body, ret="Dataset[Item] | None", models=COMPUTED_MODELS),
        extra_args=["--dataset-file", str(dataset_file)],
    )
    assert r.code == 0, r.envelope
    assert lines(dataset_file) == [{"id": "a", "shout": "A"}, {"id": "b", "shout": "B"}]


class _Interrupting:
    """A file object whose write stores part of the data and then fails."""

    def __init__(self, fh, delay: float = 0.0, fail: bool = False):
        self.fh, self.delay, self.fail = fh, delay, fail

    def write(self, view):
        time.sleep(self.delay)
        if self.fail:
            self.fh.write(bytes(view[:3]))
            raise KeyboardInterrupt
        return self.fh.write(view)

    def __getattr__(self, name):
        return getattr(self.fh, name)


def test_an_interrupted_append_leaves_only_complete_lines(tmp_path):
    from mobilerun_routines.dataset import DatasetFile

    df = DatasetFile(tmp_path / "d.jsonl")
    df.append('{"id":"a"}')
    real = df._fh
    df._fh = _Interrupting(real, fail=True)
    with pytest.raises(KeyboardInterrupt):
        df.append('{"id":"b"}')
    df._fh = real
    df.finalise()
    assert (tmp_path / "d.jsonl").read_text() == '{"id":"a"}\n'
    assert df.stats() == {"items": 1, "bytes": 11}
    df.append('{"id":"c"}')  # the file stays usable and consistent
    assert (tmp_path / "d.jsonl").read_text() == '{"id":"a"}\n{"id":"c"}\n'
    df.close()


def test_concurrent_appends_cannot_exceed_the_limit(tmp_path):
    import threading

    from mobilerun_routines import DatasetTooLargeError
    from mobilerun_routines.dataset import DatasetFile

    df = DatasetFile(tmp_path / "d.jsonl")
    df._fh = _Interrupting(df._fh, delay=0.2)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def push():
        barrier.wait()
        try:
            df.append("x" * 300_000)
            outcomes.append("ok")
        except DatasetTooLargeError:
            outcomes.append("too large")

    threads = [threading.Thread(target=push) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok", "too large"]
    assert (tmp_path / "d.jsonl").stat().st_size <= DATASET_MAX_BYTES
    assert df.stats()["items"] == 1


def test_two_threads_can_push_through_a_run(run_script, dataset_file):
    body = """
    import threading

    def worker(prefix):
        for i in range(50):
            ctx.dataset.push({"id": f"{prefix}{i}"})

    threads = [threading.Thread(target=worker, args=(p,)) for p in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    """
    r = run_script(ds_script(body), extra_args=["--dataset-file", str(dataset_file)])
    assert r.code == 0, r.envelope
    assert sorted(row["id"] for row in lines(dataset_file)) == sorted(
        f"{p}{i}" for p in "ab" for i in range(50)
    )
    assert r.envelope["dataset"]["items"] == 100


@pytest.mark.parametrize(
    "cleanup",
    ["return None", 'raise RuntimeError("cleanup failed")'],
    ids=["finally-return", "cleanup-raises"],
)
def test_cancellation_wins_over_what_the_script_does_afterwards(write_script, tmp_path, cleanup):
    body = f"""
    import time
    open(input.ready, "w").close()
    try:
        time.sleep(60)
    finally:
        {cleanup}
    """
    proc, ready, _ = _spawn(write_script(ds_script(body, ret="Out")), tmp_path, dataset=False)
    assert _terminate(proc, ready) == 1
    assert json.loads((tmp_path / "out.json").read_text()) == CANCELLED
