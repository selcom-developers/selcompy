"""Opt-in tests against the real sandbox. Skipped unless the SELCOM_SANDBOX_* variables are set."""

from __future__ import annotations

import os

import pytest

from selcompy import Client, Config, Credentials, Environment

pytestmark = pytest.mark.sandbox

_KEY = os.environ.get("SELCOM_SANDBOX_API_KEY")
_SECRET = os.environ.get("SELCOM_SANDBOX_API_SECRET")
_VENDOR = os.environ.get("SELCOM_SANDBOX_VENDOR")


@pytest.mark.skipif(not (_KEY and _SECRET and _VENDOR), reason="sandbox credentials not set")
def test_order_status_for_unknown_order_answers() -> None:
    assert _KEY
    assert _SECRET
    assert _VENDOR
    config = Config(
        environment=Environment.SANDBOX,
        credentials=Credentials(api_key=_KEY, api_secret=_SECRET, vendor=_VENDOR),
    )
    with Client(config) as client:
        result = client.order_status("ORD-DOES-NOT-EXIST-0001")
    assert result.http_status > 0
