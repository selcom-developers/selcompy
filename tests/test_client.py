from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from selcompy import (
    AsyncClient,
    Client,
    Config,
    ConfigError,
    Credentials,
    Environment,
    SigningError,
    Status,
    TransportClassification,
    TransportError,
    ValidationError,
)
from selcompy.signing import DAR_ES_SALAAM

from .helpers import CASHIN, CONFIG, CREDENTIALS, MINIMAL, ORDER, UTILITY

OK = {"resultcode": "000", "result": "SUCCESS", "message": "ok", "reference": "R1", "data": []}
FIXED = datetime(2026, 11, 21, 10, 30, 46, 999999, tzinfo=DAR_ES_SALAAM)


def recorder(body: dict[str, Any] | None = None) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body or OK)

    return httpx.MockTransport(handler), seen


def test_get_request_on_the_wire() -> None:
    transport, seen = recorder()
    with Client(CONFIG, transport=transport, clock=lambda: FIXED) as client:
        client.utility_lookup("LUKU", "A&B=C+D E/F%G~H", "TXN-LKP-0002")
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url).startswith(
        "https://apigwtest.selcommobile.com/v1/utilitypayment/lookup?"
    )
    assert request.url.query == (
        b"utilitycode=LUKU&utilityref=A%26B%3DC%2BD%20E%2FF%25G~H&transid=TXN-LKP-0002"
    )
    assert request.headers["Timestamp"] == "2026-11-21T10:30:46+03:00"
    assert request.headers["Signed-Fields"] == "utilitycode,utilityref,transid"
    assert request.headers["Digest-Method"] == "HS256"
    assert request.headers["Authorization"].startswith("SELCOM ")
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-Agent"].startswith("selcom-python/")
    assert request.content == b""
    # a query keeps the connection pool; no forced close
    assert "connection" not in {k.lower() for k in request.headers} or (
        request.headers["connection"] != "close"
    )


def test_post_sends_flat_json_in_signed_order() -> None:
    transport, seen = recorder()
    with Client(CONFIG, transport=transport, clock=lambda: FIXED) as client:
        client.create_order(ORDER)
    request = seen[0]
    body = json.loads(request.content)
    keys = list(body)
    assert keys[0] == "vendor"
    assert "billing.firstname" in body
    assert "billing" not in body
    assert request.headers["Signed-Fields"] == ",".join(keys)
    assert request.headers["Content-Type"] == "application/json"
    assert body["amount"] == 8000
    assert body["vendor"] == "TILL123"


def test_checkout_urls_are_base64_encoded() -> None:
    transport, seen = recorder()
    with Client(CONFIG, transport=transport) as client:
        from dataclasses import replace

        client.create_order_minimal(replace(MINIMAL, webhook="https://shop.example.com/hook/abc"))
    body = json.loads(seen[0].content)
    assert body["webhook"] == "aHR0cHM6Ly9zaG9wLmV4YW1wbGUuY29tL2hvb2svYWJj"


def test_payouts_carry_vendor_and_pin_from_config() -> None:
    transport, seen = recorder()
    with Client(CONFIG, transport=transport) as client:
        client.wallet_cashin(CASHIN)
        client.vendor_balance("TXN-BAL-1")
    first, second = (json.loads(r.content) for r in seen)
    assert (first["vendor"], first["pin"]) == ("TILL123", "0000")
    assert list(second) == ["vendor", "pin", "transid"]


def test_pin_is_required_for_payouts_before_anything_is_sent() -> None:
    config = Config(
        environment=Environment.SANDBOX,
        credentials=Credentials(api_key="k-0001", api_secret="s", vendor="V"),
    )
    transport, seen = recorder()
    with Client(config, transport=transport) as client:
        with pytest.raises(ConfigError):
            client.utility_payment(UTILITY)
        client.order_status("ORD-1")  # checkout needs no PIN
    assert len(seen) == 1


def test_checkout_can_use_its_own_credentials() -> None:
    config = Config(
        environment=Environment.SANDBOX,
        credentials=CREDENTIALS,
        checkout_credentials=Credentials(api_key="shop-key-9999", api_secret="s2", vendor="SHOP"),
    )
    transport, seen = recorder()
    with Client(config, transport=transport) as client:
        client.create_order_minimal(MINIMAL)
        client.vendor_balance("TXN-BAL-1")
    shop, payout = seen
    assert json.loads(shop.content)["vendor"] == "SHOP"
    assert json.loads(payout.content)["vendor"] == "TILL123"
    assert shop.headers["Authorization"] != payout.headers["Authorization"]


def test_payments_and_writes_ask_for_a_fresh_connection_and_queries_do_not() -> None:
    transport, seen = recorder()
    with Client(CONFIG, transport=transport) as client:
        client.utility_payment(UTILITY)
        client.create_order_minimal(MINIMAL)
        client.cancel_order("ORD-1")
        client.delete_card(1, "GBU")
        client.order_status("ORD-1")
        client.vendor_balance("TXN-1")
    flags = [r.headers.get("connection") for r in seen]
    assert flags[:4] == ["close"] * 4
    assert "close" not in flags[4:]


def test_timeouts_per_operation_class() -> None:
    transport, seen = recorder()
    with Client(CONFIG, transport=transport) as client:
        client.utility_payment(UTILITY)
        client.order_status("ORD-1")
    pay, query = (r.extensions["timeout"] for r in seen)
    assert pay == {"connect": 15.0, "read": 180.0, "write": 15.0, "pool": 15.0}
    assert query == {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}


def test_no_hidden_resend_and_no_redirect_following() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": "https://elsewhere.example.com/"})

    with Client(CONFIG, transport=httpx.MockTransport(handler)) as client:
        result = client.utility_payment(UTILITY)
    assert calls == 1
    assert result.status is Status.AMBIGUOUS


NOT_SENT = [
    httpx.ConnectError("refused"),
    httpx.ConnectTimeout("slow"),
    httpx.PoolTimeout("busy"),
    httpx.ProxyError("no"),
]
UNKNOWN = [
    httpx.ReadTimeout("slow"),
    httpx.WriteTimeout("slow"),
    httpx.RemoteProtocolError("cut"),
    httpx.ReadError("reset"),
    httpx.WriteError("reset"),
    httpx.LocalProtocolError("odd"),
    httpx.UnsupportedProtocol("odd"),
    OSError("something else"),
    RuntimeError("anything unclassified"),
]


def failing(exc: Exception) -> tuple[httpx.MockTransport, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise exc

    return httpx.MockTransport(handler), calls


@pytest.mark.parametrize("exc", NOT_SENT, ids=lambda e: type(e).__name__)
def test_connect_phase_failures_are_not_sent(exc: Exception) -> None:
    transport, calls = failing(exc)
    with Client(CONFIG, transport=transport) as client, pytest.raises(TransportError) as raised:
        client.utility_payment(UTILITY)
    assert raised.value.classification is TransportClassification.NOT_SENT
    assert raised.value.not_sent
    assert len(calls) == 1  # never retried


@pytest.mark.parametrize("exc", UNKNOWN, ids=lambda e: type(e).__name__)
def test_everything_else_is_unknown_outcome(exc: Exception) -> None:
    transport, calls = failing(exc)
    with Client(CONFIG, transport=transport) as client, pytest.raises(TransportError) as raised:
        client.wallet_cashin(CASHIN)
    assert raised.value.classification is TransportClassification.UNKNOWN_OUTCOME
    assert raised.value.outcome_unknown
    assert len(calls) == 1


def test_transport_errors_carry_no_request_data() -> None:
    transport, _ = failing(httpx.ConnectError("could not reach 255700000001"))
    with Client(CONFIG, transport=transport) as client, pytest.raises(TransportError) as raised:
        client.wallet_cashin(CASHIN)
    error = raised.value
    assert "255700000001" not in str(error) + repr(error)
    assert error.__cause__ is None
    assert error.cause_type == "ConnectError"


async def test_async_classification_and_no_retry() -> None:
    transport, calls = failing(httpx.ReadTimeout("slow"))
    async with AsyncClient(CONFIG, transport=transport) as client:
        with pytest.raises(TransportError) as raised:
            await client.utility_payment(UTILITY)
    assert raised.value.outcome_unknown
    assert len(calls) == 1


async def test_async_request_matches_sync_request() -> None:
    sync_transport, sync_seen = recorder()
    async_transport, async_seen = recorder()
    with Client(CONFIG, transport=sync_transport, clock=lambda: FIXED) as client:
        client.create_order(ORDER)
    async with AsyncClient(CONFIG, transport=async_transport, clock=lambda: FIXED) as aclient:
        await aclient.create_order(ORDER)
    a, b = sync_seen[0], async_seen[0]
    assert (a.url, a.content) == (b.url, b.content)
    assert a.headers["Digest"] == b.headers["Digest"]


def test_injected_http_client_is_used_and_not_closed() -> None:
    transport, seen = recorder()
    http = httpx.Client(transport=transport)
    with Client(CONFIG, http_client=http) as client:
        client.order_status("ORD-1")
    assert len(seen) == 1
    assert not http.is_closed
    http.close()


def test_http_client_and_transport_together_are_refused() -> None:
    transport, _ = recorder()
    with pytest.raises(ConfigError):
        Client(CONFIG, http_client=httpx.Client(), transport=transport)


def test_owned_client_is_closed() -> None:
    client = Client(CONFIG)
    client.close()
    with pytest.raises(RuntimeError):
        client._http.get("https://example.com/")


def test_signing_failure_sends_nothing() -> None:
    from selcompy.operations import Plan, order_status

    transport, seen = recorder()
    plan = order_status("ORD-1")
    bad = Plan(plan.spec, {"bad,key": "x"}, plan.parse)
    with Client(CONFIG, transport=transport) as client, pytest.raises(SigningError):
        client._execute(bad)
    assert seen == []


def test_validation_failures_send_nothing() -> None:
    transport, seen = recorder()
    with Client(CONFIG, transport=transport) as client:
        with pytest.raises(ValidationError):
            client.order_status("")
        with pytest.raises(ValidationError):
            client.list_orders("21/11/2026", "2026-11-21")
        with pytest.raises(ValidationError):
            client.create_order_minimal(MINIMAL.__class__(**{**MINIMAL.__dict__, "amount": 80.5}))
    assert seen == []


def test_result_values_are_typed() -> None:
    body = {
        "resultcode": "000",
        "data": [
            {
                "gateway_buyer_uuid": "GBU-1",
                "payment_token": "800",
                "qr": "QR",
                "payment_gateway_url": "aHR0cHM6Ly9jaGVja291dC5leGFtcGxlLmNvbS9wYXkvT1JELTEwMDU=",
            }
        ],
    }
    transport, _ = recorder(body)
    with Client(CONFIG, transport=transport) as client:
        result = client.create_order(ORDER)
    assert result.value is not None
    assert result.value.payment_gateway_url == "https://checkout.example.com/pay/ORD-1005"
    assert (result.value.payment_gateway_url_raw or "").startswith("aHR0")
    assert result.order_id == "ORD-1001"
    assert result.is_final()


def test_order_status_verdict_comes_from_payment_status() -> None:
    body = {
        "resultcode": "000",
        "data": [
            {
                "order_id": "ORD-1",
                "amount": "10,000",
                "payment_status": "completed",
                "msisdn": "255700000001",
            }
        ],
    }
    transport, _ = recorder(body)
    with Client(CONFIG, transport=transport) as client:
        result = client.order_status("ORD-1")
    assert result.status is Status.SUCCESS
    assert result.value is not None
    assert (result.value.amount_value, result.value.phone) == (10000, "255700000001")
