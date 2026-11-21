"""The I/O-free half of the clients: build one request, read one response.

``Client`` and ``AsyncClient`` only add the HTTP call and the transport error handling.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from ._version import __version__
from .config import Config, Timeouts
from .errors import (
    ConfigError,
    InvalidResponseError,
    TransportClassification,
    TransportError,
)
from .operations import Plan
from .results import OperationKind, Result, classify
from .signing import DAR_ES_SALAAM, HS256Signer, Signer, format_timestamp, sign_request

Clock = Callable[[], datetime]
SignerFactory = Callable[[str], Signer]

USER_AGENT = f"selcom-python/{__version__}"

# Failures that happen while the connection is being made. Nothing has been written yet.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


PAYMENT_LIMITS = httpx.Limits(max_keepalive_connections=0)
"""Pool limits for the client that carries payments and writes: no connection is kept for reuse."""


def check_injected(client: httpx.Client | httpx.AsyncClient) -> None:
    """Refuse injected clients whose defaults would change the signed request."""
    if len(client.params) > 0 or client.auth is not None:
        raise ConfigError(
            "an injected client must not set default params or auth: they change the signed request"
        )


def check_payment_client(client: httpx.Client | httpx.AsyncClient) -> None:
    """Refuse a payment client that could carry a payment on a connection already used.

    This reads the pool settings of httpx's own transports (a private detail of httpx and
    httpcore); transports without such a pool, such as test fakes, are not inspected.
    """
    missing = object()
    transports = [client._transport, *client._mounts.values()]
    for transport in transports:
        pool = getattr(transport, "_pool", missing)
        if pool is missing and not isinstance(
            transport, (httpx.HTTPTransport, httpx.AsyncHTTPTransport)
        ):
            continue  # a fake or custom transport without an httpx pool: not inspected
        http2 = getattr(pool, "_http2", missing)
        keepalive = getattr(pool, "_max_keepalive_connections", missing)
        if missing in (http2, keepalive):
            # Fail closed: a pool whose settings cannot be read cannot be vouched for.
            raise ConfigError(
                "payment_http_client has a connection pool whose keep-alive and HTTP/2 settings "
                "cannot be read; this version of httpx is not supported for injected clients"
            )
        if http2:
            raise ConfigError(
                "payment_http_client must not enable HTTP/2: it multiplexes requests on one "
                "connection (build it with http2=False)"
            )
        if keepalive:
            raise ConfigError(
                "payment_http_client must not keep connections for reuse "
                "(build it with limits=PAYMENT_LIMITS)"
            )


def check_transport(transport: object) -> None:
    """A real httpx transport pools connections, so it cannot carry payments by itself."""
    if isinstance(transport, (httpx.HTTPTransport, httpx.AsyncHTTPTransport)):
        raise ConfigError(
            "transport= takes fakes and custom transports, not httpx's own HTTP transports: "
            "pass http_client with a dedicated payment_http_client instead"
        )


def default_clock() -> datetime:
    """The current instant in Africa/Dar_es_Salaam (+03:00, no daylight saving)."""
    return datetime.now(DAR_ES_SALAAM)


def default_signer(secret: str) -> Signer:
    return HS256Signer(secret)


def classify_exception(exc: BaseException) -> TransportClassification:
    """NOT_SENT only for connect-phase failures. Everything else, including anything this
    function does not recognise, is UNKNOWN_OUTCOME."""
    if isinstance(exc, _NOT_SENT):
        return TransportClassification.NOT_SENT
    return TransportClassification.UNKNOWN_OUTCOME


def transport_error(operation: str, exc: BaseException) -> TransportError:
    return TransportError(classify_exception(exc), operation, type(exc).__name__)


@dataclass(frozen=True, slots=True, repr=False)
class Prepared:
    plan: Plan
    method: str
    url: str
    headers: dict[str, str]
    content: bytes | None
    timeout: httpx.Timeout

    def __repr__(self) -> str:
        """Headers, query and body hold the signature, the PIN and personal data: not shown."""
        return f"Prepared(operation={self.plan.spec.name!r}, method={self.method!r})"


def _httpx_timeout(t: Timeouts) -> httpx.Timeout:
    return httpx.Timeout(connect=t.connect, read=t.read, write=t.connect, pool=t.connect)


def decode_json_object(raw: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, dict) else None


class Core:
    def __init__(
        self,
        config: Config,
        clock: Clock | None = None,
        signer_factory: SignerFactory | None = None,
    ) -> None:
        self.config = config
        self._clock = clock or default_clock
        self._signer_factory = signer_factory or default_signer

    def prepare(self, plan: Plan) -> Prepared:
        """Validate, sign and assemble one request. Raises before anything is sent."""
        spec = plan.spec
        creds = self.config.credentials_for(spec.checkout)
        params: dict[str, Any] = {}
        if spec.needs_vendor:
            params["vendor"] = creds.vendor
        if spec.needs_pin:
            if creds.pin is None:
                raise ConfigError(f"{spec.name} needs the float PIN in the credentials")
            params["pin"] = creds.pin
        params.update(plan.params)

        timestamp = format_timestamp(self._clock())
        signed = sign_request(
            spec.method, params, timestamp, creds.api_key, self._signer_factory(creds.api_secret)
        )
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **signed.headers}
        url = self.config.root_url + spec.path
        content: bytes | None = None
        if signed.body is not None:
            headers["Content-Type"] = "application/json"
            content = signed.body.encode("utf-8")
        if signed.query:
            url = f"{url}?{signed.query}"
        if spec.fresh_connection:
            headers["Connection"] = "close"
        timeouts = (
            self.config.payment_timeouts
            if spec.kind is OperationKind.PAYMENT
            else self.config.query_timeouts
        )
        return Prepared(plan, spec.method, url, headers, content, _httpx_timeout(timeouts))

    def finish(self, plan: Plan, http_status: int, raw_bytes: bytes) -> Result[Any]:
        """Turn one HTTP response into a typed result."""
        spec = plan.spec
        raw = raw_bytes.decode("utf-8", errors="replace")
        body = decode_json_object(raw)
        status, code = classify(spec.kind, body)
        if status is None:
            raise InvalidResponseError(spec.name, http_status, raw)
        fields: dict[str, Any] = body or {}
        data = fields.get("data")
        entries = [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
        try:
            value = plan.parse(entries)
        except Exception:  # typed parsing must never turn a verdict into an error
            value = None

        def text(key: str) -> str | None:
            item = fields.get(key)
            return item if isinstance(item, str) else None

        return Result(
            operation=spec.name,
            status=status,
            http_status=http_status,
            resultcode=code,
            result=text("result"),
            message=text("message"),
            reference=text("reference"),
            transid=plan.transid or text("transid"),
            order_id=plan.order_id,
            data=entries,
            value=value,
            raw=raw,
        )
