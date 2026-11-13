"""Device access: build the `mobilerun-core` device of a run from the sandbox environment."""

from __future__ import annotations

import importlib
import os
from typing import Any

from .errors import NotAvailableError

# Set by the platform. The key is a placeholder; the platform attaches the real credential
# to outbound API requests, so this package never handles a real one.
API_BASE_URL_ENV = "MOBILERUN_API_BASE_URL"
API_KEY_ENV = "MOBILERUN_API_KEY"
# Older platform executors set this name; it is read only when `API_KEY_ENV` is unset or empty.
LEGACY_API_KEY_ENV = "MOBILERUN_CLOUD_API_KEY"


def _missing(reason: str) -> NotAvailableError:
    return NotAvailableError("device", reason)


def connect_device(device_id: str | None) -> Any:
    """Connect to `device_id` and return the `mobilerun_core` device unchanged."""
    if not device_id:
        raise _missing("is not available: this run has no device")
    try:
        core = importlib.import_module("mobilerun_core")
    except ImportError:
        raise _missing(
            "is not available: mobilerun-core is not installed "
            "(install mobilerun-routines[device])"
        ) from None
    base_url = os.environ.get(API_BASE_URL_ENV)
    api_key = os.environ.get(API_KEY_ENV) or os.environ.get(LEGACY_API_KEY_ENV)
    absent = [
        name
        for name, value in ((API_BASE_URL_ENV, base_url), (API_KEY_ENV, api_key))
        if not value
    ]
    if absent:
        raise _missing(f"is not available: {' and '.join(absent)} not set")
    client = core.Mobilerun(bearer=api_key, base_url=base_url)
    return client.connect(device_id, cloud=True)
