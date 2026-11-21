from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from selcompy import (
    Client,
    Config,
    ConfigError,
    Credentials,
    Environment,
    HS256Signer,
    InvalidResponseError,
    Timeouts,
    WebhookVerifier,
    redact,
    redact_body,
    redact_headers,
    redact_query,
    sign_request,
)

from .helpers import CASHIN, CONFIG, CREDENTIALS, ORDER


def test_credentials_repr_masks_key_and_hides_secret_and_pin() -> None:
    text = repr(CREDENTIALS) + str(CREDENTIALS)
    assert "test-key-0001" not in text
    assert "0001" in text  # last 4 characters of the key
    assert "test-secret-0001" not in text
    assert "'0000'" not in text
    assert "pin=<hidden>" in text


def test_config_client_signer_and_verifier_reprs_hide_secrets() -> None:
    text = " ".join(
        [
            repr(CONFIG),
            repr(Client(CONFIG)),
            repr(HS256Signer("test-secret-0001")),
            repr(WebhookVerifier("test-secret-0001")),
        ]
    )
    assert "test-secret-0001" not in text
    assert "test-key-0001" not in text


def test_signed_request_repr_hides_digest_and_authorization() -> None:
    signed = sign_request(
        "GET",
        {"order_id": "ORD-1"},
        "2026-11-21T10:30:46+03:00",
        "test-key-0001",
        HS256Signer("test-secret-0001"),
    )
    text = repr(signed)
    assert signed.digest not in text
    assert signed.authorization not in text


def test_request_reprs_mask_personal_data_and_cashin_utilityref() -> None:
    text = repr(ORDER) + repr(CASHIN)
    for private in ("buyer@example.com", "Test Buyer", "255700000001", "Test Street"):
        assert private not in text or private == "Test Street"
    assert "255700000001" not in repr(CASHIN)
    assert "********0001" in repr(CASHIN)


def test_result_repr_has_no_body_data() -> None:
    body = {
        "resultcode": "000",
        "message": "paid by 255700000001",
        "data": [{"phone": "255700000001"}],
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    with Client(CONFIG, transport=transport) as client:
        result = client.order_status("ORD-1")
    assert "255700000001" not in repr(result)
    assert result.raw
    assert result.data


def test_exceptions_hide_the_raw_body() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(502, content=b"<html>255700000001"))
    with Client(CONFIG, transport=transport) as client, pytest.raises(InvalidResponseError) as e:
        client.vendor_balance("TXN-1")
    assert "255700000001" not in str(e.value) + repr(e.value)
    assert "255700000001" in e.value.raw_body


def test_redact_helper_follows_the_spec_table() -> None:
    data = {
        "pin": "1234",
        "api_secret": "s",
        "Authorization": "SELCOM abc",
        "card_token": "tok",
        "msisdn": "255700000001",
        "buyer_phone": "255700000002",
        "buyer_email": "test@example.com",
        "buyer_name": "Test Buyer",
        "billing": {"firstname": "John", "lastname": "Doe", "phone": "255700000003"},
        "billing.lastname": "Doe",
        "utilityref": "255700000004",
        "amount": 5000,
        "order_id": "ORD-1",
    }
    out = redact(data)
    assert (
        out["pin"] == out["api_secret"] == out["Authorization"] == out["card_token"] == "[REDACTED]"
    )
    assert out["msisdn"] == "********0001"
    assert out["buyer_email"] == "t***@example.com"
    assert out["buyer_name"] == "T***"
    assert out["billing"] == {"firstname": "J***", "lastname": "D***", "phone": "********0003"}
    assert out["billing.lastname"] == "D***"
    assert out["utilityref"] == "255700000004"  # not on a cash-in path
    assert out["amount"] == 5000
    assert redact(data, "/v1/walletcashin/process")["utilityref"] == "********0004"
    assert redact([data])[0]["pin"] == "[REDACTED]"


def test_redact_headers_query_and_body() -> None:
    headers = redact_headers({"authorization": "SELCOM x", "DIGEST": "d", "Timestamp": "t"})
    assert headers == {"authorization": "[REDACTED]", "DIGEST": "[REDACTED]", "Timestamp": "t"}
    query = redact_query("?utilityref=255700000001&transid=T1", "/v1/walletcashin/namelookup")
    assert query == "utilityref=%2A%2A%2A%2A%2A%2A%2A%2A0001&transid=T1"
    body = json.loads(redact_body('{"pin":"1","billing.phone":"255700000001"}'))
    assert body == {"pin": "[REDACTED]", "billing.phone": "********0001"}
    assert redact_body("<html>255700000001</html>") == "[UNPARSEABLE BODY REDACTED]"
    assert redact_query("") == ""


def test_configuration_fails_early_and_has_no_default_environment() -> None:
    with pytest.raises(TypeError):
        Config(credentials=CREDENTIALS)  # type: ignore[call-arg]
    with pytest.raises(ConfigError):
        Config(environment="staging", credentials=CREDENTIALS)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        Credentials(api_key="", api_secret="s", vendor="V")
    with pytest.raises(ConfigError):
        Credentials(api_key="k", api_secret="", vendor="V")
    with pytest.raises(ConfigError):
        Credentials(api_key="k", api_secret="s", vendor="")
    with pytest.raises(ConfigError):
        Timeouts(connect=0, read=1)


def test_environments_and_custom_base_url() -> None:
    sandbox = Config(environment="sandbox", credentials=CREDENTIALS)  # type: ignore[arg-type]
    assert sandbox.root_url == "https://apigwtest.selcommobile.com"
    production = replace(sandbox, environment=Environment.PRODUCTION)
    assert production.root_url == "https://apigw.selcommobile.com"
    with pytest.raises(ConfigError):
        Config(
            environment=Environment.SANDBOX,
            credentials=CREDENTIALS,
            base_url="https://x.example.com",
        )
    with pytest.raises(ConfigError):
        Config(environment=Environment.CUSTOM, credentials=CREDENTIALS)
    with pytest.raises(ConfigError):
        Config(
            environment=Environment.CUSTOM,
            credentials=CREDENTIALS,
            base_url="http://sim.example.com",
        )
    custom = Config(
        environment=Environment.CUSTOM, credentials=CREDENTIALS, base_url="http://localhost:8080/"
    )
    assert custom.root_url == "http://localhost:8080"
