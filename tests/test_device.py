from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from mobilerun_routines import Context, NotAvailableError
from mobilerun_routines.context import RunInfo
from mobilerun_routines.device import (
    API_BASE_URL_ENV,
    API_KEY_ENV,
    LEGACY_API_BASE_URL_ENV,
    LEGACY_API_KEY_ENV,
)

SECRET = "sk-placeholder-secret-value"
BASE_URL = "https://api.example.test"


def make_ctx(device_id: str | None = "dev-1") -> Context:
    run = RunInfo(run_id="r", attempt=1, deadline=None, mode="live", device_id=device_id)
    return Context(run)


class FakeCore:
    def __init__(self) -> None:
        self.clients: list[dict[str, Any]] = []
        self.connects: list[tuple[str, dict[str, Any]]] = []
        self.device = object()
        core = self

        class Mobilerun:
            def __init__(self, *, bearer: str | None = None, base_url: str | None = None) -> None:
                core.clients.append({"bearer": bearer, "base_url": base_url})

            def connect(self, device_id: str, **kwargs: Any) -> object:
                core.connects.append((device_id, kwargs))
                return core.device

        self.module = types.ModuleType("mobilerun_core")
        self.module.Mobilerun = Mobilerun  # type: ignore[attr-defined]


@pytest.fixture
def fake_core(monkeypatch: pytest.MonkeyPatch) -> FakeCore:
    core = FakeCore()
    monkeypatch.setitem(sys.modules, "mobilerun_core", core.module)
    monkeypatch.setenv(API_BASE_URL_ENV, BASE_URL)
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    monkeypatch.delenv(LEGACY_API_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(LEGACY_API_KEY_ENV, raising=False)
    return core


def test_no_device_id_raises(fake_core: FakeCore) -> None:
    with pytest.raises(NotAvailableError, match="no device"):
        make_ctx(None).device()
    assert fake_core.connects == []


@pytest.mark.parametrize("missing", [API_BASE_URL_ENV, API_KEY_ENV])
@pytest.mark.parametrize("empty", [False, True])
def test_missing_env_names_the_variable(
    fake_core: FakeCore, monkeypatch: pytest.MonkeyPatch, missing: str, empty: bool
) -> None:
    if empty:
        monkeypatch.setenv(missing, "")
    else:
        monkeypatch.delenv(missing)
    with pytest.raises(NotAvailableError) as info:
        make_ctx().device()
    assert missing in str(info.value)
    assert SECRET not in str(info.value)
    assert fake_core.connects == []


def test_new_key_name_wins_over_the_legacy_one(
    fake_core: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LEGACY_API_KEY_ENV, "legacy-value")
    make_ctx().device()
    assert fake_core.clients == [{"bearer": SECRET, "base_url": BASE_URL}]


@pytest.mark.parametrize("empty", [False, True])
def test_legacy_key_name_alone_works(
    fake_core: FakeCore, monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    if empty:
        monkeypatch.setenv(API_KEY_ENV, "")
    else:
        monkeypatch.delenv(API_KEY_ENV)
    monkeypatch.setenv(LEGACY_API_KEY_ENV, "legacy-value")
    make_ctx().device()
    assert fake_core.clients == [{"bearer": "legacy-value", "base_url": BASE_URL}]


def test_new_base_url_name_wins_over_the_legacy_one(
    fake_core: FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LEGACY_API_BASE_URL_ENV, "https://legacy.example.test")
    make_ctx().device()
    assert fake_core.clients == [{"bearer": SECRET, "base_url": BASE_URL}]


@pytest.mark.parametrize("empty", [False, True])
def test_legacy_base_url_name_alone_works(
    fake_core: FakeCore, monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    if empty:
        monkeypatch.setenv(API_BASE_URL_ENV, "")
    else:
        monkeypatch.delenv(API_BASE_URL_ENV)
    monkeypatch.setenv(LEGACY_API_BASE_URL_ENV, "https://legacy.example.test")
    make_ctx().device()
    assert fake_core.clients == [{"bearer": SECRET, "base_url": "https://legacy.example.test"}]


def test_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mobilerun_core", None)  # makes the import fail
    monkeypatch.setenv(API_BASE_URL_ENV, BASE_URL)
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    with pytest.raises(NotAvailableError, match="mobilerun-core is not installed") as info:
        make_ctx().device()
    assert SECRET not in str(info.value)


def test_happy_path_returns_library_object(fake_core: FakeCore) -> None:
    device = make_ctx("dev-42").device()
    assert device is fake_core.device
    assert fake_core.clients == [{"bearer": SECRET, "base_url": BASE_URL}]
    assert fake_core.connects == [("dev-42", {"cloud": True})]


def test_connects_once_per_run(fake_core: FakeCore) -> None:
    ctx = make_ctx()
    assert ctx.device() is ctx.device()
    assert len(fake_core.connects) == 1


def test_connect_failure_is_not_cached(fake_core: FakeCore) -> None:
    ctx = make_ctx()
    original = fake_core.module.Mobilerun.connect  # type: ignore[attr-defined]

    def boom(self: Any, device_id: str, **kwargs: Any) -> object:
        raise RuntimeError("down")

    fake_core.module.Mobilerun.connect = boom  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        ctx.device()
    fake_core.module.Mobilerun.connect = original  # type: ignore[attr-defined]
    assert ctx.device() is fake_core.device


def test_other_members_still_unavailable() -> None:
    ctx = make_ctx()
    for name in ("credentials", "files", "llm"):
        with pytest.raises(NotAvailableError):
            getattr(ctx, name)


def local_ctx(device_id: str | None = None, **kw: Any) -> Context:
    run = RunInfo(
        run_id="r", attempt=1, deadline=None, mode="live", device_id=device_id,
        device_target="local", **kw,
    )  # fmt: skip
    return Context(run)


@pytest.fixture
def fake_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "mobilerun_core_local", types.ModuleType("x"))


def test_local_adb_serial_has_no_bearer_and_needs_no_cloud_env(
    fake_core: FakeCore, fake_local: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(API_BASE_URL_ENV)
    monkeypatch.delenv(API_KEY_ENV)
    device = local_ctx("emulator-5554").device()
    assert device is fake_core.device
    assert fake_core.clients == [{"bearer": None, "base_url": None}]
    assert fake_core.connects == [("emulator-5554", {"backend": "local-android-adb", "url": None})]


def test_local_android_never_opens_a_cloud_device(fake_core: FakeCore, fake_local: None) -> None:
    uuid = "11111111-2222-3333-4444-555555555555"
    local_ctx(uuid, device_platform="android").device()
    assert fake_core.connects == [(uuid, {"backend": "local-android-adb", "url": None})]
    assert all(c["bearer"] is None for c in fake_core.clients)


def test_local_android_url(fake_core: FakeCore, fake_local: None) -> None:
    local_ctx(None, device_platform="android", device_url="http://h:8080").device()
    assert fake_core.connects == [(None, {"backend": "local-android-http", "url": "http://h:8080"})]


def test_local_ios_url(fake_core: FakeCore, fake_local: None) -> None:
    local_ctx(None, device_platform="ios", device_url="http://10.0.0.5:6643").device()
    assert fake_core.connects == [
        (None, {"backend": "local-ios-http", "url": "http://10.0.0.5:6643"})
    ]


def test_local_without_device_or_url_uses_core_message(
    fake_core: FakeCore, fake_local: None
) -> None:
    def boom(self: Any, device_id: str | None, **kwargs: Any) -> object:
        raise ValueError("local-android-adb connect requires an ADB serial")

    fake_core.module.Mobilerun.connect = boom  # type: ignore[attr-defined]
    with pytest.raises(NotAvailableError, match="requires an ADB serial"):
        local_ctx(None).device()


def test_local_core_value_error_is_one_line(fake_core: FakeCore, fake_local: None) -> None:
    def boom(self: Any, device_id: str | None, **kwargs: Any) -> object:
        raise ValueError("local-android-http connect requires\nurl=...")

    fake_core.module.Mobilerun.connect = boom  # type: ignore[attr-defined]
    with pytest.raises(NotAvailableError) as info:
        local_ctx("x").device()
    assert "\n" not in str(info.value)
    assert "requires url=..." in str(info.value)


@pytest.mark.parametrize("missing", ["mobilerun_core", "mobilerun_core_local"])
def test_local_backend_missing_names_extra(
    fake_core: FakeCore, fake_local: None, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setitem(sys.modules, missing, None)
    with pytest.raises(NotAvailableError, match=r"mobilerun-routines\[local\]"):
        local_ctx("x").device()
    assert fake_core.connects == []


SCRIPT = """
from pydantic import BaseModel
from mobilerun_routines import Context, script

class In(BaseModel):
    pass

@script
def main(input: In, ctx: Context) -> None:
    ctx.device()
"""
CLOUD_UUID = "11111111-2222-3333-4444-555555555555"


def test_executor_argv_is_the_cloud_call(fake_core: FakeCore, run_script: Any) -> None:
    result = run_script(SCRIPT, extra_args=["--device-id", CLOUD_UUID])
    assert result.code == 0 and result.envelope["ok"] is True
    assert fake_core.clients == [{"bearer": SECRET, "base_url": BASE_URL}]
    assert fake_core.connects == [(CLOUD_UUID, {"cloud": True})]
    assert SECRET not in json.dumps(result.envelope) + result.err


def test_cli_local_flags(fake_core: FakeCore, fake_local: None, run_script: Any) -> None:
    result = run_script(
        SCRIPT,
        extra_args=[
            "--device-target", "local", "--device-platform", "ios", "--device-url", "http://h:1",
        ],
    )  # fmt: skip
    assert result.code == 0 and result.envelope["ok"] is True
    assert fake_core.connects == [(None, {"backend": "local-ios-http", "url": "http://h:1"})]
    assert SECRET not in json.dumps(result.envelope) + result.err


@pytest.mark.parametrize("flags", [["--device-url", "http://h:1"], ["--device-platform", "ios"]])
def test_local_only_flags_need_local_target(
    fake_core: FakeCore, run_script: Any, capsys: Any, flags: list[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        run_script(SCRIPT, extra_args=["--device-id", CLOUD_UUID, *flags])
    assert info.value.code == 64  # the parser's usual usage-error exit
    assert "--device-target local" in capsys.readouterr().err
    assert fake_core.connects == []
