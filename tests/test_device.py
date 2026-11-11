from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from mobilerun_sandbox import Context, NotAvailableError
from mobilerun_sandbox.context import RunInfo
from mobilerun_sandbox.device import API_BASE_URL_ENV, API_KEY_ENV

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
            def __init__(self, *, bearer: str, base_url: str) -> None:
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
