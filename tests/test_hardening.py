from __future__ import annotations

import asyncio
import copy
import http.server
import json
import pickle
import threading
import traceback
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from pathlib import Path

import httpx
import pytest

from selcompy import (
    PAYMENT_LIMITS,
    AsyncClient,
    Client,
    Config,
    ConfigError,
    Credentials,
    Environment,
    HS256Signer,
    InMemoryReplayStore,
    ReplayStoreError,
    Timeouts,
    TransportError,
    Verdict,
    WebhookVerifier,
    parse_webhook,
    sign_request,
)
from selcompy._core import Core
from selcompy.operations import utility_payment, wallet_cashin

from .helpers import CASHIN, UTILITY
from .test_webhooks import NOW, SECRET, delivery

PIN = "PIN-98765"
SECRET_KEY = "secret-value-424242"
CONFIG = Config(
    environment=Environment.SANDBOX,
    credentials=Credentials(
        api_key="test-key-0001", api_secret=SECRET_KEY, vendor="TILL123", pin=PIN
    ),
)
TS = "2026-11-21T10:30:46+03:00"


# 1. Nothing sensitive survives in exception context or captured locals ---------------------


def _failing_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    return httpx.MockTransport(handler)


def _rendered(error: BaseException) -> str:
    return "".join(traceback.TracebackException.from_exception(error, capture_locals=True).format())


def _cashin_error() -> TransportError:
    with Client(CONFIG, transport=_failing_transport()) as client:
        try:
            client.wallet_cashin(CASHIN)
        except TransportError as error:
            return error
    raise AssertionError("no error raised")


def test_transport_error_has_no_cause_and_no_context() -> None:
    error = _cashin_error()
    assert error.__cause__ is None
    assert error.__context__ is None


def test_traceback_with_captured_locals_shows_no_secret_or_personal_data() -> None:
    text = _rendered(_cashin_error())
    assert "_execute" in text  # the locals of the client frame really were captured
    for private in (PIN, SECRET_KEY, "255700000001", "SELCOM ", "Digest", "test-key-0001"):
        assert private not in text


async def test_async_error_has_no_context_and_no_secret_locals() -> None:
    async with AsyncClient(CONFIG, transport=_failing_transport()) as client:
        try:
            await client.wallet_cashin(CASHIN)
        except TransportError as error:
            caught = error
    assert caught.__cause__ is None
    assert caught.__context__ is None
    text = _rendered(caught)
    assert "_execute" in text
    for private in (PIN, SECRET_KEY, "255700000001", "SELCOM "):
        assert private not in text


def test_plan_and_prepared_reprs_hide_secrets_and_personal_data() -> None:
    core = Core(CONFIG)
    plan = utility_payment(replace(UTILITY, msisdn="255700000009"))
    prepared = core.prepare(plan)
    text = repr(plan) + repr(prepared) + str(prepared)
    for private in (PIN, SECRET_KEY, "255700000009", prepared.headers["Digest"], "SELCOM "):
        assert private not in text
    assert "utilitypayment.process" in text
    cashin = core.prepare(wallet_cashin(CASHIN))
    assert "255700000001" not in repr(cashin) + repr(cashin.plan)
    assert "********0001" in repr(cashin.plan)


# 2. Payments and writes really use their own, non-reused connections -----------------------


class _Server:
    def __init__(self) -> None:
        self.ports: list[tuple[str, int]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.ports.append((self.path.split("?")[0], self.client_address[1]))
                body = json.dumps({"resultcode": "000", "data": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_DELETE = _answer

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def config(self) -> Config:
        return Config(
            environment=Environment.CUSTOM,
            credentials=CONFIG.credentials,
            base_url=f"http://127.0.0.1:{self.httpd.server_address[1]}",
        )

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[_Server]:
    s = _Server()
    yield s
    s.stop()


def _check_ports(ports: list[tuple[str, int]]) -> None:
    queries = [p for path, p in ports if path.endswith("order-status")]
    money = [p for path, p in ports if path.endswith(("process", "cancel-order"))]
    assert len(queries) == 3
    assert len(money) == 3
    assert len(set(queries)) == 1  # queries do keep their connection alive
    assert len(set(money)) == 3  # every payment and write opened a new one
    assert set(money).isdisjoint(queries)  # and none reused a query's connection


def test_payments_and_writes_use_fresh_connections_sync(server: _Server) -> None:
    with Client(server.config) as client:
        client.order_status("ORD-1")
        client.utility_payment(UTILITY)
        client.order_status("ORD-1")
        client.utility_payment(replace(UTILITY, transid="TXN-UTL-0002"))
        client.cancel_order("ORD-1")
        client.order_status("ORD-1")
    _check_ports(server.ports)


async def test_payments_and_writes_use_fresh_connections_async(server: _Server) -> None:
    async with AsyncClient(server.config) as client:
        await client.order_status("ORD-1")
        await client.utility_payment(UTILITY)
        await client.order_status("ORD-1")
        await client.utility_payment(replace(UTILITY, transid="TXN-UTL-0002"))
        await client.cancel_order("ORD-1")
        await client.order_status("ORD-1")
    _check_ports(server.ports)


def test_injected_clients_need_a_dedicated_payment_client() -> None:
    http_client = httpx.Client()
    with pytest.raises(ConfigError, match="payment_http_client"):
        Client(CONFIG, http_client=http_client)
    with pytest.raises(ConfigError, match="payment_http_client"):
        Client(CONFIG, http_client=http_client, payment_http_client=http_client)
    with pytest.raises(ConfigError):
        Client(CONFIG, payment_http_client=httpx.Client())
    http_client.close()


async def test_async_injected_clients_need_a_dedicated_payment_client() -> None:
    http_client = httpx.AsyncClient()
    with pytest.raises(ConfigError, match="payment_http_client"):
        AsyncClient(CONFIG, http_client=http_client)
    with pytest.raises(ConfigError, match="payment_http_client"):
        AsyncClient(CONFIG, http_client=http_client, payment_http_client=http_client)
    await http_client.aclose()


# 4. Injected clients must not change the signed request ------------------------------------

BAD_CLIENTS: list[Callable[[], httpx.Client]] = [
    lambda: httpx.Client(params={"x": "1"}),
    lambda: httpx.Client(auth=("user", "pass")),
]


@pytest.mark.parametrize("make", BAD_CLIENTS)
def test_injected_client_with_params_or_auth_is_refused(make: Callable[[], httpx.Client]) -> None:
    with pytest.raises(ConfigError, match="params or auth"):
        Client(CONFIG, http_client=make(), payment_http_client=httpx.Client())
    with pytest.raises(ConfigError, match="params or auth"):
        Client(CONFIG, http_client=httpx.Client(), payment_http_client=make())


async def test_async_injected_client_with_params_or_auth_is_refused() -> None:
    with pytest.raises(ConfigError, match="params or auth"):
        AsyncClient(
            CONFIG,
            http_client=httpx.AsyncClient(params={"x": "1"}),
            payment_http_client=httpx.AsyncClient(),
        )
    with pytest.raises(ConfigError, match="params or auth"):
        AsyncClient(
            CONFIG,
            http_client=httpx.AsyncClient(),
            payment_http_client=httpx.AsyncClient(auth=("u", "p")),
        )


# 3. Enum and subclass values sign as plain values ------------------------------------------


class Code(str, Enum):
    LUKU = "LUKU"


class Count(IntEnum):
    THREE = 3


def test_str_and_int_subclasses_sign_and_send_as_plain_values() -> None:
    signer = HS256Signer("s")
    plain = sign_request("POST", {"utilitycode": "LUKU", "n": 3}, TS, "k-0001", signer)
    enums = sign_request("POST", {"utilitycode": Code.LUKU, "n": Count.THREE}, TS, "k-0001", signer)
    assert enums.signing_string == plain.signing_string
    assert enums.digest == plain.digest
    assert enums.body == plain.body == '{"utilitycode":"LUKU","n":3}'
    assert type(enums.flattened[0][1]) is str
    assert type(enums.flattened[1][1]) is int
    query = sign_request("GET", {"utilitycode": Code.LUKU, "n": Count.THREE}, TS, "k-0001", signer)
    assert query.query == "utilitycode=LUKU&n=3"
    assert query.signing_string.endswith("utilitycode=LUKU&n=3")


def test_enum_keys_are_plain_text_too() -> None:
    class Key(str, Enum):
        A = "alpha"

    signed = sign_request("GET", {Key.A: "x"}, TS, "k-0001", HS256Signer("s"))
    assert signed.signed_fields == "alpha"
    assert signed.query == "alpha=x"


# 5. Secrets do not pickle, copies are the same object --------------------------------------


@pytest.mark.parametrize(
    "obj",
    [CONFIG.credentials, CONFIG, HS256Signer(SECRET_KEY)],
    ids=["Credentials", "Config", "HS256Signer"],
)
def test_secret_holders_refuse_pickling_and_copy_as_themselves(obj: object) -> None:
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError, match="secrets"):
            pickle.dumps(obj, protocol=protocol)
    assert copy.copy(obj) is obj
    assert copy.deepcopy(obj) is obj
    assert copy.deepcopy({"k": obj})["k"] is obj


def test_replace_still_works_on_config() -> None:
    assert replace(CONFIG, environment=Environment.PRODUCTION).environment is Environment.PRODUCTION


# 6. Webhooks -------------------------------------------------------------------------------


class AsyncStore:
    def __init__(self) -> None:
        self.keys: dict[str, datetime] = {}
        self.calls = 0

    async def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool:
        await asyncio.sleep(0)
        self.calls += 1
        if key in self.keys and self.keys[key] > now:
            return True
        self.keys[key] = expires_at
        return False


async def test_averify_with_async_store() -> None:
    store = AsyncStore()
    v = WebhookVerifier(SECRET, clock=lambda: NOW, async_replay_store=store)
    headers, body = delivery()
    assert await v.averify(headers, body) is Verdict.VALID
    assert await v.averify(headers, body) is Verdict.REPLAY
    assert store.calls == 2
    assert await v.averify({}, body) is Verdict.UNSIGNED
    assert await v.averify(headers, "nope") is Verdict.INVALID
    assert max(store.keys.values()) == NOW + timedelta(seconds=300)


async def test_averify_falls_back_to_the_sync_store() -> None:
    v = WebhookVerifier(SECRET, clock=lambda: NOW, replay_store=InMemoryReplayStore())
    headers, body = delivery()
    assert await v.averify(headers, body) is Verdict.VALID
    assert v.verify(headers, body) is Verdict.REPLAY


async def test_averify_store_failure_is_a_replay_store_error() -> None:
    class Broken:
        async def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool:
            raise ConnectionError("down")

    v = WebhookVerifier(SECRET, clock=lambda: NOW, async_replay_store=Broken())
    with pytest.raises(ReplayStoreError):
        await v.averify(*delivery())


def test_str_body_cap_counts_utf8_bytes() -> None:
    headers, _ = delivery()
    # 600 000 characters, 1 200 000 bytes: under the cap by characters, over it by bytes
    wide = '{"transid":"T","order_id":"O","pad":"' + "é" * 600_000 + '"}'
    assert len(wide) < 1_048_576 < len(wide.encode("utf-8"))
    v = WebhookVerifier(SECRET, clock=lambda: NOW)
    assert v.verify(headers, wide) is Verdict.INVALID
    assert v.verify(headers, wide.encode("utf-8")) is Verdict.INVALID
    assert parse_webhook(wide) is None
    assert parse_webhook(wide.encode("utf-8")) is None
    assert parse_webhook('{"order_id":"O","pad":"' + "é" * 1000 + '"}') is not None


def test_naive_clock_is_a_configuration_error_at_construction() -> None:
    with pytest.raises(ConfigError, match="timezone"):
        WebhookVerifier(SECRET, clock=lambda: datetime(2026, 11, 21, 10, 31))
    flip = [NOW]
    verifier = WebhookVerifier(SECRET, clock=lambda: flip[0])
    flip[0] = datetime(2026, 11, 21, 10, 31)  # a clock that turns naive later still raises
    headers, body = delivery()
    with pytest.raises(ConfigError, match="timezone"):
        verifier.verify(headers, body)
    with pytest.raises(ConfigError, match="timezone"):
        asyncio.run(verifier.averify(headers, body))
    aware = WebhookVerifier(
        SECRET, clock=lambda: datetime(2026, 11, 21, 7, 31, tzinfo=timezone.utc)
    )
    assert aware.verify(headers, body) is Verdict.VALID


def test_async_store_as_replay_store_is_refused_at_construction() -> None:
    with pytest.raises(ConfigError, match="async_replay_store"):
        WebhookVerifier(SECRET, replay_store=AsyncStore())  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="async function"):
        WebhookVerifier(SECRET, async_replay_store=InMemoryReplayStore())  # type: ignore[arg-type]


class _Answers:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool:
        return self.answer  # type: ignore[return-value]


class _LateCoroutine:
    """Not an async function, but it hands back a coroutine: also not a bool."""

    async def _inner(self) -> bool:
        return False

    def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool:
        return self._inner()  # type: ignore[return-value]


@pytest.mark.parametrize("answer", [1, 0, "yes", None, [], object()])
def test_a_store_must_answer_with_a_bool(answer: object) -> None:
    v = WebhookVerifier(SECRET, clock=lambda: NOW, replay_store=_Answers(answer))
    with pytest.raises(ReplayStoreError):
        v.verify(*delivery())
    with pytest.raises(ReplayStoreError):
        asyncio.run(v.averify(*delivery()))


def test_a_store_returning_a_coroutine_is_a_store_error_not_a_replay() -> None:
    v = WebhookVerifier(SECRET, clock=lambda: NOW, replay_store=_LateCoroutine())
    with pytest.raises(ReplayStoreError):
        v.verify(*delivery())


async def test_async_store_must_answer_with_a_bool_too() -> None:
    class Wrong:
        async def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool:
            return "no"  # type: ignore[return-value]

    v = WebhookVerifier(SECRET, clock=lambda: NOW, async_replay_store=Wrong())
    with pytest.raises(ReplayStoreError):
        await v.averify(*delivery())


@pytest.mark.parametrize("attribute", ["_http2", "_max_keepalive_connections", "_pool"])
def test_unreadable_pool_settings_fail_closed(attribute: str) -> None:
    client = httpx.Client(limits=PAYMENT_LIMITS)
    target = client._transport if attribute == "_pool" else client._transport._pool  # type: ignore[attr-defined]
    delattr(target, attribute)  # as if httpcore had renamed it
    with pytest.raises(ConfigError, match="cannot be read"):
        Client(CONFIG, http_client=httpx.Client(), payment_http_client=client)


async def test_async_unreadable_pool_settings_fail_closed() -> None:
    client = httpx.AsyncClient(limits=PAYMENT_LIMITS)
    del client._transport._pool._http2  # type: ignore[attr-defined]
    with pytest.raises(ConfigError, match="cannot be read"):
        AsyncClient(CONFIG, http_client=httpx.AsyncClient(), payment_http_client=client)


# 2b. The transport= path and payment clients with pooling or HTTP/2 -------------------------


def test_real_httpx_transports_are_refused_in_transport() -> None:
    with pytest.raises(ConfigError, match="payment_http_client"):
        Client(CONFIG, transport=httpx.HTTPTransport())
    with pytest.raises(ConfigError, match="payment_http_client"):
        AsyncClient(CONFIG, transport=httpx.AsyncHTTPTransport())
    Client(CONFIG, transport=httpx.MockTransport(lambda r: httpx.Response(200))).close()


def test_payment_client_that_keeps_connections_is_refused() -> None:
    pooled = httpx.Client()
    with pytest.raises(ConfigError, match="PAYMENT_LIMITS"):
        Client(CONFIG, http_client=httpx.Client(), payment_http_client=pooled)
    proxied = httpx.Client(proxy="http://proxy.example.com:3128")
    with pytest.raises(ConfigError, match="PAYMENT_LIMITS"):
        Client(CONFIG, http_client=httpx.Client(), payment_http_client=proxied)


def test_payment_client_with_http2_is_refused() -> None:
    client = httpx.Client(limits=PAYMENT_LIMITS)
    client._transport._pool._http2 = True  # type: ignore[attr-defined]  # as http2=True sets it
    with pytest.raises(ConfigError, match="HTTP/2"):
        Client(CONFIG, http_client=httpx.Client(), payment_http_client=client)


async def test_async_payment_client_checks() -> None:
    with pytest.raises(ConfigError, match="PAYMENT_LIMITS"):
        AsyncClient(
            CONFIG, http_client=httpx.AsyncClient(), payment_http_client=httpx.AsyncClient()
        )
    client = httpx.AsyncClient(limits=PAYMENT_LIMITS)
    client._transport._pool._http2 = True  # type: ignore[attr-defined]
    with pytest.raises(ConfigError, match="HTTP/2"):
        AsyncClient(CONFIG, http_client=httpx.AsyncClient(), payment_http_client=client)


def test_properly_built_injected_clients_keep_payments_on_fresh_connections(
    server: _Server,
) -> None:
    queries = httpx.Client()
    payments = httpx.Client(limits=PAYMENT_LIMITS, http2=False)
    with Client(server.config, http_client=queries, payment_http_client=payments) as client:
        client.order_status("ORD-1")
        client.utility_payment(UTILITY)
        client.order_status("ORD-1")
        client.utility_payment(replace(UTILITY, transid="TXN-UTL-0002"))
        client.cancel_order("ORD-1")
        client.order_status("ORD-1")
    queries.close()
    payments.close()
    _check_ports(server.ports)


async def test_properly_built_async_injected_clients_use_fresh_connections(
    server: _Server,
) -> None:
    queries = httpx.AsyncClient()
    payments = httpx.AsyncClient(limits=PAYMENT_LIMITS, http2=False)
    async with AsyncClient(
        server.config, http_client=queries, payment_http_client=payments
    ) as client:
        await client.order_status("ORD-1")
        await client.utility_payment(UTILITY)
        await client.order_status("ORD-1")
        await client.utility_payment(replace(UTILITY, transid="TXN-UTL-0002"))
        await client.cancel_order("ORD-1")
        await client.order_status("ORD-1")
    await queries.aclose()
    await payments.aclose()
    _check_ports(server.ports)


# 7. Config and packaging -------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 0, -1, 0.0, True, "10", None])
def test_timeouts_reject_non_finite_and_non_positive_values(bad: object) -> None:
    with pytest.raises(ConfigError):
        Timeouts(connect=bad, read=1.0)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        Timeouts(connect=1.0, read=bad)  # type: ignore[arg-type]
    assert Timeouts(connect=1, read=2.5).read == 2.5


def test_pyproject_keeps_the_licence_expression_and_no_licence_classifier() -> None:
    text = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "Apache-2.0"' in text
    assert "License ::" not in text
