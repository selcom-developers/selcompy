# selcompy

Typed Python client for the Selcom API gateway, with a synchronous `Client` and an asynchronous `AsyncClient` over [httpx](https://www.python-httpx.org/). Apache-2.0.

> **Not affiliated with Selcom Paytech Ltd.** This is a community project. "Selcom" names the API it talks to. For API access, credentials, contracts and support, go to Selcom: <https://developers.selcommobile.com/>. Where this client and Selcom's portal disagree about the API itself, the portal wins.

It is written from the shared specification in [selcom-spec](https://github.com/selcom-developers/selcom-spec) and tested against that specification's vectors, so it signs requests and reads results the same way as the other community clients.

Every layer is usable on its own and replaceable: the signer, the transport, the operations, the results and the webhook verifier.

## Install

```
pip install selcompy
```

Python 3.10 or later. The only runtime dependency is `httpx`.

## Quickstart

```python
from selcompy import (
    Client,
    Config,
    Credentials,
    Environment,
    Status,
    UtilityPaymentRequest,
)

config = Config(
    environment=Environment.SANDBOX,  # no default: you always choose
    credentials=Credentials(
        api_key="...",
        api_secret="...",
        vendor="TILL123",
        pin="...",
    ),
)

with Client(config) as client:
    result = client.utility_payment(
        UtilityPaymentRequest(
            transid="PAYOUT-1001",  # your own deterministic ID
            utilitycode="LUKU",
            utilityref="54100000001",
            amount=5000,  # whole TZS
        )
    )
    if result.status is Status.SUCCESS:
        print(result.value.receipt)
```

The asynchronous client has the same operations; every method is a coroutine.

```python
from selcompy import AsyncClient

async with AsyncClient(config) as client:
    result = await client.order_status("ORD-1001")
```

## Configuration

| Setting | Notes |
|---|---|
| `environment` | `Environment.SANDBOX`, `Environment.PRODUCTION` or `Environment.CUSTOM` (with `base_url`, for a simulator or a proxy). There is no default, and a missing value is an error |
| `credentials` | `Credentials(api_key, api_secret, vendor, pin=None)`. The PIN is only needed for payouts |
| `checkout_credentials` | Optional second set, used for checkout operations when checkout and payouts have separate credentials |
| `query_timeouts` | Connect 10 s, read 10 s: queries, order status and the other operations |
| `payment_timeouts` | Connect 15 s, read 180 s: money-moving operations. The long read timeout is deliberate, because timing out early does not stop a payment, it only turns a known answer into an unknown one |

A missing key, secret or vendor fails when the configuration is built, not on the first request. Printing a `Config`, `Credentials`, `Client`, signer or verifier shows only the last 4 characters of the API key and never the secret or the PIN.

## Operations

| Method | Operation |
|---|---|
| `create_order(CreateOrderRequest)` | `checkout.create-order` |
| `create_order_minimal(CreateOrderMinimalRequest)` | `checkout.create-order-minimal` |
| `wallet_payment(WalletPaymentRequest)` | `checkout.wallet-payment` |
| `card_payment(CardPaymentRequest)` | `checkout.card-payment` |
| `order_status(order_id)` | `checkout.order-status` |
| `cancel_order(order_id)` | `checkout.cancel-order` |
| `list_orders(fromdate, todate)` | `checkout.list-orders` |
| `stored_cards(gateway_buyer_uuid, buyer_userid)` | `checkout.stored-cards` |
| `delete_card(card_id, gateway_buyer_uuid)` | `checkout.delete-card` |
| `utility_payment(UtilityPaymentRequest)` | `utilitypayment.process` |
| `utility_lookup(utilitycode, utilityref, transid)` | `utilitypayment.lookup` |
| `utility_query(transid)` | `utilitypayment.query` |
| `wallet_cashin(WalletCashInRequest)` | `walletcashin.process` |
| `wallet_name_lookup(utilitycode, utilityref, transid)` | `walletcashin.namelookup` |
| `wallet_cashin_query(transid)` | `walletcashin.query` |
| `vendor_balance(transid)` | `vendor.balance` |

Requests are typed, frozen dataclasses. The vendor ID and the float PIN come from the configuration. Amounts are whole units: an `int` or a whole `Decimal`, never a float, and a fractional amount is rejected before signing. Checkout orders always carry an explicit `currency`. Webhook, redirect and cancel URLs are given as plain URLs; the client base64-encodes them as the API requires.

Submitting and querying are separate methods. Nothing hides a query inside a submit.

## Results and errors

Every operation returns a `Result` with:

- `status`: `SUCCESS`, `PENDING`, `AMBIGUOUS`, `FAILED` or `NOT_FOUND`, from the result code as the specification defines;
- `resultcode`, `result`, `message`, `reference`, `transid` and `order_id`;
- `http_status`, the decoded `data` entries and the raw response body in `raw`;
- `value`, the typed form of `data` for operations that return something (for example `OrderCreated`, `OrderStatusData`, `StoredCard`);
- `is_final()`, true for `SUCCESS`, `FAILED` and `NOT_FOUND`.

`data` and `raw` can hold personal data and are left out of `repr`. Redact them before logging.

Three kinds of failure never look alike:

| Situation | What you get |
|---|---|
| The gateway answered | a `Result`, whatever the HTTP status. The result code decides the verdict |
| The answer carries no verdict (an error page, an empty body) | for payments and status queries, a `Result` with `AMBIGUOUS`; for other operations, `InvalidResponseError` |
| No answer at all | `TransportError` with a classification |

`TransportError.classification` is `NOT_SENT` only when the failure happened while connecting (name resolution, refused connection, connect timeout, handshake, proxy, exhausted pool). Everything else, including read and write timeouts, resets and anything unrecognised, is `UNKNOWN_OUTCOME`. Treat `UNKNOWN_OUTCOME` on a payment as `AMBIGUOUS`: query, never resend.

`ConfigError`, `ValidationError` and `SigningError` are raised before anything is sent.

## Idempotency and reconciliation

The client never retries, not even a connect failure on a payment or a write. `transid` and `order_id` are your idempotency keys, and you decide what happens after an unknown outcome.

1. Generate IDs deterministically from your own records (for example the payout row's primary key with a prefix), and persist the ID in a "submitting" state before you call the gateway.
2. Put a unique constraint on the ID so that a retried job cannot create a second transaction.
3. On `PENDING`, `AMBIGUOUS` or an `UNKNOWN_OUTCOME` error, wait at least 3 minutes, then query with the same ID (`utility_query`, `wallet_cashin_query`, or `order_status` for checkout). Repeat every 3 minutes for a bounded window you choose, then hand the transaction to a person.
4. Never create a new ID while an outcome is unknown. Use a new ID only after the old one is `FAILED`.
5. Submit the same ID again only after a status query returns `NOT_FOUND`, or after `NOT_SENT`.
6. Apply results through one function keyed on the ID, under a row lock or a compare-and-set, so that a webhook and a poll arriving together cannot both win.

Payments and writes (create, cancel, delete) never reuse a connection. By default the client owns a separate pool for them, built with `max_keepalive_connections=0` and without HTTP/2, so each one opens a new connection and closes it after the response. (The `Connection: close` header is sent too, but it only takes effect after the response and HTTP/2 drops it, so it is not what provides this.)

## Replacing the transport

The HTTP layer is yours to replace. Pass a transport (a fake in tests), or your own ready clients (proxy, logging, event hooks, custom CA bundle):

```python
import httpx
from selcompy import PAYMENT_LIMITS, Client

# A fake transport for tests
fake = httpx.MockTransport(lambda request: httpx.Response(200, json={"resultcode": "000"}))
client = Client(config, transport=fake)

# Your own clients: one for queries, and a dedicated one for payments and writes
queries = httpx.Client(proxy="http://proxy.internal:3128", verify="/etc/ssl/ca.pem")
payments = httpx.Client(
    proxy="http://proxy.internal:3128",
    verify="/etc/ssl/ca.pem",
    limits=PAYMENT_LIMITS,   # keep no connection for reuse
    http2=False,             # HTTP/2 multiplexes requests on one connection
)
client = Client(config, http_client=queries, payment_http_client=payments)  # you close both
```

When you pass `http_client` you must also pass a separate `payment_http_client`, built as above, so that a payment or write never travels on a connection an earlier request used. The same client twice is refused. Injected clients must not set default `params` or `auth`, because those would change the signed request; that is refused too.

The default transports are built with `retries=0` and redirects are never followed. If you inject something that retries on its own, do not use it for payments or writes.

The clock and the signer are replaceable too: `Client(config, clock=..., signer_factory=...)`. The signer in `selcompy.signing` is a pure function (`sign_request`) with no I/O and no clock.

## Webhooks

A webhook is a hint, not proof. Confirm with `order_status` before you fulfil anything, and take the amount from there.

```python
from selcompy import Verdict, WebhookVerifier, parse_webhook, ReplayStoreError

verifier = WebhookVerifier(api_secret)  # window_seconds=300, in-memory replay store


def handle(headers: dict[str, str], raw_body: bytes) -> int:
    try:
        verdict = verifier.verify(headers, raw_body)  # use the raw body, before any parsing
    except ReplayStoreError:
        return 503  # the delivery will be repeated
    if verdict in (Verdict.INVALID, Verdict.STALE):
        return 401
    if verdict is Verdict.REPLAY:
        return 200  # already handled
    payload = parse_webhook(raw_body)
    store(payload, redact_body(raw_body))  # 1. store
    return 200  # 2. acknowledge; 3. confirm later
```

Handler order: verify, store, acknowledge, then confirm with `order_status` in the background. Return a 5xx only when storing fails.

`verify` follows the specification step by step. It never raises on bad input: every malformed delivery is `INVALID`, and numbers in signed fields are checked from their text without being expanded. The one exception is `ReplayStoreError`, raised when the replay store itself fails. `UNSIGNED` deliveries have nothing to check, so the order-status confirmation is the only protection.

In an async handler use `await verifier.averify(headers, raw_body)`. It accepts an `async_replay_store` (the `AsyncReplayStore` protocol, for a shared store) and falls back to the synchronous store otherwise. The verifier's clock must return a timezone-aware `datetime`; a naive one raises `ConfigError`. Bodies over 1 MiB (counted in UTF-8 bytes) are `INVALID`.

A replay entry lives until the later of the current time and the signed timestamp, plus the window. `InMemoryReplayStore` is for one process; implement the `ReplayStore` protocol (one atomic method) on a shared store when you run several.

`parse_webhook` never raises. Fields it cannot read come back as `None`, and `amount_tzs` is `None` ("unparsed") when the amount text is not a whole-unit amount. `VALID` covers only the fields named in `Signed-Fields`, and the amount is not one of them.

## Security

- Secrets: the API secret, the PIN, `Authorization` and `Digest` never appear in `repr`, exception messages or the library's own output. The API key shows its last 4 characters.
- Redaction: `redact`, `redact_headers`, `redact_query` and `redact_body` mask secrets, phone numbers (last 4 characters kept), e-mail addresses and names, and treat `utilityref` as a phone number on `/v1/walletcashin/` paths. Use them for anything you store or log.
- Exceptions carry no request: no URL, headers or body, and no chained exception that references one. Credentials, configuration and signers refuse to be pickled.
- Keep credentials out of source control and load them from the environment or a secret store.
- Keep the host clock synchronised: the gateway rejects requests whose time is too far from its own.

To report a problem in this library, see [SECURITY.md](SECURITY.md).

## Specification and tests

The tests run every signing case and every rejection, every response scenario and every webhook sample from a copy of the specification's vectors in `tests/vectors`. `SPEC_VERSION` records the specification tag the copy comes from, and `python scripts/refresh_vectors.py` refreshes it (`--check` compares byte for byte, as CI does). No test touches the network. Tests against the real sandbox are opt-in: set `SELCOM_SANDBOX_API_KEY`, `SELCOM_SANDBOX_API_SECRET` and `SELCOM_SANDBOX_VENDOR`, then run `pytest -m sandbox`.

## Development

```
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE). Copyright The Selcom Developers community contributors.
