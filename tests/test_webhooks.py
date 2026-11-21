from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from selcompy import (
    HS256Signer,
    InMemoryReplayStore,
    ReplayStoreError,
    Verdict,
    WebhookVerifier,
    parse_webhook,
)
from selcompy.signing import signing_string

SECRET = "test-secret-0001"
NOW = datetime.fromisoformat("2026-11-21T10:31:00+03:00")
TS = "2026-11-21T10:30:46+03:00"
FIELDS = "transid,order_id,reference,result,resultcode,payment_status"


def delivery(
    body: dict[str, Any] | str | None = None,
    fields: str = FIELDS,
    timestamp: str = TS,
    secret: str = SECRET,
    raw: str | None = None,
) -> tuple[dict[str, str], str]:
    body = body or {
        "transid": "TXN-1",
        "order_id": "ORD-1",
        "reference": "REF1",
        "result": "SUCCESS",
        "resultcode": "000",
        "payment_status": "COMPLETED",
        "amount": "10,000",
    }
    text = raw if raw is not None else json.dumps(body)
    parsed = json.loads(text, parse_int=str, parse_float=str)
    flat = []
    for name in fields.split(","):
        value = parsed.get(name, "")
        flat.append((name, "" if value is None else str(value)))
    digest = HS256Signer(secret).sign(signing_string(timestamp, flat))
    headers = {
        "Digest": digest,
        "Digest-Method": "HS256",
        "Timestamp": timestamp,
        "Signed-Fields": fields,
    }
    return headers, text


def verifier(**kwargs: Any) -> WebhookVerifier:
    return WebhookVerifier(SECRET, clock=lambda: NOW, **kwargs)


def test_valid_then_replay() -> None:
    v = verifier()
    headers, body = delivery()
    assert v.verify(headers, body) is Verdict.VALID
    assert v.verify(headers, body) is Verdict.REPLAY


def test_header_names_are_case_insensitive() -> None:
    headers, body = delivery()
    lowered = {k.lower(): val for k, val in headers.items()}
    assert verifier().verify(lowered, body) is Verdict.VALID


def test_unsigned_and_basic_invalids() -> None:
    v = verifier()
    headers, body = delivery()
    assert v.verify({}, body) is Verdict.UNSIGNED
    assert v.verify({**headers, "Digest": ""}, body) is Verdict.INVALID
    assert v.verify({**headers, "Digest-Method": "RS256"}, body) is Verdict.INVALID
    assert v.verify({**headers, "Timestamp": "yesterday"}, body) is Verdict.INVALID
    assert v.verify({**headers, "Signed-Fields": ""}, body) is Verdict.INVALID
    assert v.verify(headers, "[1,2]") is Verdict.INVALID
    assert v.verify(headers, "not json") is Verdict.INVALID
    assert v.verify(headers, b"\xff\xfe") is Verdict.INVALID


def test_stale_in_both_directions() -> None:
    old = delivery(timestamp="2026-11-21T09:30:46+03:00")
    ahead = delivery(timestamp="2026-11-21T11:30:46+03:00")
    assert verifier().verify(*old) is Verdict.STALE
    assert verifier().verify(*ahead) is Verdict.STALE


def test_signed_fields_must_include_order_id_and_transid() -> None:
    assert verifier().verify(*delivery(fields="transid,reference")) is Verdict.INVALID
    assert verifier().verify(*delivery(fields="order_id,reference")) is Verdict.INVALID


def test_ampersand_in_signed_name_or_value_is_invalid() -> None:
    body = {"transid": "T&1", "order_id": "O1"}
    assert verifier().verify(*delivery(body, fields="transid,order_id")) is Verdict.INVALID


@pytest.mark.parametrize(
    "number",
    [
        "1e100000000",
        "1e-999999999",
        "-1e999999999",
        "8000.5",
        "9007199254740993",
        "1" + "0" * 5000,
        "0." + "0" * 5000 + "1",
        "NaN",
        "Infinity",
        "1E+999999999999999999999999",
    ],
)
def test_hostile_numbers_in_signed_fields_are_invalid_and_cheap(number: str) -> None:
    raw = f'{{"transid":"T1","order_id":"O1","amount":{number}}}'
    headers, _ = delivery({"transid": "T1", "order_id": "O1"}, fields="transid,order_id")
    headers["Signed-Fields"] = "transid,order_id,amount"
    assert verifier().verify(headers, raw) is Verdict.INVALID


@pytest.mark.parametrize(
    ("number", "signed"),
    [
        ("8000", "8000"),
        ("8000.0", "8000"),
        ("8e3", "8000"),
        ("-5", "-5"),
        ("0", "0"),
        ("80E+2", "8000"),
    ],
)
def test_whole_numbers_sign_like_requests(number: str, signed: str) -> None:
    raw = f'{{"transid":"T1","order_id":"O1","amount":{number}}}'
    fields = "transid,order_id,amount"
    flat = [("transid", "T1"), ("order_id", "O1"), ("amount", signed)]
    digest = HS256Signer(SECRET).sign(signing_string(TS, flat))
    headers = {"Digest": digest, "Digest-Method": "HS256", "Timestamp": TS, "Signed-Fields": fields}
    assert verifier().verify(headers, raw) is Verdict.VALID


def test_verify_never_raises_on_odd_input() -> None:
    v = verifier()
    headers, _ = delivery()
    deep = "[" * 100000
    for body in ("", "{", deep, "{" * 5000, '{"a":' * 3000, "\x00", '{"transid":[1]}'):
        assert v.verify(headers, body) is Verdict.INVALID
    assert v.verify({"Digest": 5}, "{}") is Verdict.INVALID  # type: ignore[dict-item]
    assert v.verify(headers, "x" * 2_000_000) is Verdict.INVALID


def test_nested_dotted_field_is_found() -> None:
    body = {"transid": "T1", "order_id": "O1", "billing": {"firstname": "Test"}}
    fields = "transid,order_id,billing.firstname"
    flat = [("transid", "T1"), ("order_id", "O1"), ("billing.firstname", "Test")]
    digest = HS256Signer(SECRET).sign(signing_string(TS, flat))
    headers = {"Digest": digest, "Digest-Method": "HS256", "Timestamp": TS, "Signed-Fields": fields}
    assert verifier().verify(headers, json.dumps(body)) is Verdict.VALID


def test_replay_entry_lives_until_the_later_of_now_and_timestamp_plus_window() -> None:
    class Spy:
        def __init__(self) -> None:
            self.expiry: datetime | None = None

        def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool:
            self.expiry = expires_at
            return False

    spy = Spy()
    # signed 4 minutes ahead of the receiver's clock: still inside the 300 s window
    ahead = (NOW + timedelta(seconds=240)).isoformat(timespec="seconds")
    assert (
        WebhookVerifier(SECRET, clock=lambda: NOW, replay_store=spy).verify(
            *delivery(timestamp=ahead)
        )
        is Verdict.VALID
    )
    assert spy.expiry == NOW + timedelta(seconds=240) + timedelta(seconds=300)
    # signed in the past: arrival time counts
    assert (
        WebhookVerifier(SECRET, clock=lambda: NOW, replay_store=spy).verify(*delivery())
        is Verdict.VALID
    )
    assert spy.expiry == NOW + timedelta(seconds=300)


def test_replay_entry_expires() -> None:
    store = InMemoryReplayStore()
    clock = [NOW]
    v = WebhookVerifier(SECRET, clock=lambda: clock[0], replay_store=store, window_seconds=300)
    headers, body = delivery()
    assert v.verify(headers, body) is Verdict.VALID
    clock[0] = NOW + timedelta(seconds=280)
    assert v.verify(headers, body) is Verdict.REPLAY
    clock[0] = NOW + timedelta(seconds=301)  # outside the window now: STALE wins before store
    assert v.verify(headers, body) is Verdict.STALE
    assert len(store) == 1


def test_store_failure_is_the_only_exception() -> None:
    class Broken:
        def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool:
            raise ConnectionError("down")

    with pytest.raises(ReplayStoreError):
        verifier(replay_store=Broken()).verify(*delivery())


def test_full_in_memory_store_fails_instead_of_forgetting() -> None:
    store = InMemoryReplayStore(max_entries=1)
    v = verifier(replay_store=store)
    assert v.verify(*delivery()) is Verdict.VALID
    other = delivery({"transid": "TXN-2", "order_id": "ORD-2"}, fields="transid,order_id")
    with pytest.raises(ReplayStoreError):
        v.verify(*other)


def test_parse_webhook_never_throws() -> None:
    assert parse_webhook("nope") is None
    assert parse_webhook("[]") is None
    assert parse_webhook(b"\xff") is None
    assert parse_webhook("[" * 100000) is None
    p = parse_webhook('{"order_id":"O1","amount":1e999999999,"phone":["x"],"result":5}')
    assert p is not None
    assert p.order_id == "O1"
    assert p.amount_tzs is None
    assert p.phone is None
    assert p.result == "5"


def test_parse_webhook_amounts() -> None:
    def amount(text: str) -> int | None:
        payload = parse_webhook(json.dumps({"amount": text}))
        assert payload is not None
        return payload.amount_tzs

    assert amount("10,000") == 10000
    assert amount("10000.00") == 10000
    assert amount("1,0000") is None
    assert amount("10.5") is None
    assert amount("-5") is None
    assert amount("9" * 60) is None
    payload = parse_webhook('{"amount":10000,"msisdn":"255700000001"}')
    assert payload is not None
    assert (payload.amount_tzs, payload.phone) == (10000, "255700000001")


def test_payload_repr_hides_personal_data() -> None:
    payload = parse_webhook('{"order_id":"O1","phone":"255700000001","amount":"1"}')
    assert payload is not None
    assert "255700000001" not in repr(payload)
    assert "0001" in repr(payload)
