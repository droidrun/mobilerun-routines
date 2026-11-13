"""Device access: build the `mobilerun-core` device of a run from the sandbox environment."""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any

from .errors import NotAvailableError

if TYPE_CHECKING:
    from .context import DevicePlatform, DeviceTarget

# Set by the platform. The key is a placeholder; the platform attaches the real credential
# to outbound API requests, so this package never handles a real one.
API_BASE_URL_ENV = "MOBILERUN_BASE_URL"
# Older platform executors set this name; it is read only when `API_BASE_URL_ENV` is unset or empty.
LEGACY_API_BASE_URL_ENV = "MOBILERUN_API_BASE_URL"
API_KEY_ENV = "MOBILERUN_API_KEY"
# Older platform executors set this name; it is read only when `API_KEY_ENV` is unset or empty.
LEGACY_API_KEY_ENV = "MOBILERUN_CLOUD_API_KEY"


_LOCAL_HINT = (
    "is not available: the local device backend is not installed "
    "(install mobilerun-routines[local])"
)


def _missing(reason: str) -> NotAvailableError:
    return NotAvailableError("device", reason)


def connect_device(
    device_id: str | None,
    *,
    target: DeviceTarget = "cloud",
    platform: DevicePlatform | None = None,
    url: str | None = None,
) -> Any:
    """Connect to the run's device and return the `mobilerun_core` device unchanged."""
    if target == "local":
        return _connect_local(device_id, platform, url)
    return _connect_cloud(device_id)


def _local_backend(platform: str | None, url: str | None) -> str:
    if platform == "ios":
        return "local-ios-http"
    return "local-android-http" if url else "local-android-adb"


def _connect_local(device_id: str | None, platform: str | None, url: str | None) -> Any:
    try:
        core = importlib.import_module("mobilerun_core")
        importlib.import_module("mobilerun_core_local")
    except ImportError:
        raise _missing(_LOCAL_HINT) from None
    try:
        # An explicit backend means a local run can never reach the cloud. No bearer: local
        # devices need none, and Portal tokens come from core's own environment variables.
        client = core.Mobilerun()
        return client.connect(device_id, backend=_local_backend(platform, url), url=url)
    except ValueError as exc:
        raise _missing(f"is not available: {' '.join(str(exc).split())}") from None


def _connect_cloud(device_id: str | None) -> Any:
    if not device_id:
        raise _missing("is not available: this run has no device")
    try:
        core = importlib.import_module("mobilerun_core")
    except ImportError:
        raise _missing(
            "is not available: mobilerun-core is not installed (install mobilerun-routines[device])"
        ) from None
    base_url = os.environ.get(API_BASE_URL_ENV) or os.environ.get(LEGACY_API_BASE_URL_ENV)
    api_key = os.environ.get(API_KEY_ENV) or os.environ.get(LEGACY_API_KEY_ENV)
    absent = [
        name for name, value in ((API_BASE_URL_ENV, base_url), (API_KEY_ENV, api_key)) if not value
    ]
    if absent:
        raise _missing(f"is not available: {' and '.join(absent)} not set")
    client = core.Mobilerun(bearer=api_key, base_url=base_url)
    return client.connect(device_id, cloud=True)
