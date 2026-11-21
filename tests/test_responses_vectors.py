from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from selcompy import AsyncClient, Client, InvalidResponseError

from .conftest import RESPONSE_FILES
from .helpers import CALLS, CONFIG

SCENARIOS = [
    pytest.param(op, sc, id=f"{op}:{sc['name']}")
    for op, doc in RESPONSE_FILES.items()
    for sc in doc["scenarios"]
]


def _response(scenario: dict[str, Any]) -> httpx.Response:
    if "raw_body" in scenario:
        return httpx.Response(scenario["http_status"], content=scenario["raw_body"].encode())
    return httpx.Response(scenario["http_status"], content=json.dumps(scenario["body"]).encode())


def test_every_operation_has_a_call() -> None:
    assert set(CALLS) == set(RESPONSE_FILES)
    assert len(RESPONSE_FILES) == 16


@pytest.mark.parametrize(("operation", "scenario"), SCENARIOS)
def test_scenario_sync(operation: str, scenario: dict[str, Any]) -> None:
    transport = httpx.MockTransport(lambda request: _response(scenario))
    with Client(CONFIG, transport=transport) as client:
        if "expected_error" in scenario:
            assert scenario["expected_error"] == "INVALID_RESPONSE"
            with pytest.raises(InvalidResponseError) as raised:
                CALLS[operation](client)
            assert raised.value.http_status == scenario["http_status"]
            return
        result = CALLS[operation](client)
    assert result.status.name == scenario["expected_status"]
    assert result.http_status == scenario["http_status"]
    assert result.operation == operation


@pytest.mark.parametrize(("operation", "scenario"), SCENARIOS)
async def test_scenario_async(operation: str, scenario: dict[str, Any]) -> None:
    transport = httpx.MockTransport(lambda request: _response(scenario))
    async with AsyncClient(CONFIG, transport=transport) as client:
        if "expected_error" in scenario:
            with pytest.raises(InvalidResponseError):
                await CALLS[operation](client)
            return
        result = await CALLS[operation](client)
    assert result.status.name == scenario["expected_status"]
