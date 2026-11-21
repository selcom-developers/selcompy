"""The synchronous client."""

from __future__ import annotations

from datetime import date
from types import TracebackType
from typing import Any

import httpx

from . import operations as _ops
from ._core import PAYMENT_LIMITS, Clock, Core, SignerFactory, check_injected, transport_error
from .config import Config
from .errors import ConfigError, TransportError
from .models import (
    Balance,
    CardPaymentRequest,
    CreateOrderMinimalRequest,
    CreateOrderRequest,
    LookupData,
    OrderCreated,
    OrderStatusData,
    OrderSummary,
    PaymentData,
    StoredCard,
    UtilityPaymentRequest,
    WalletCashInRequest,
    WalletPaymentRequest,
)
from .results import Result


class Client:
    """Typed operations over httpx.

    The transport is replaceable. Pass a custom transport as ``transport`` (a fake in tests), or
    your own ready clients as ``http_client`` (queries and lookups) together with
    ``payment_http_client`` (payments and writes). Clients you pass in are yours to close.

    Nothing is retried, ever: not by this class, and the default transports are built with
    ``retries=0``. If you inject something that retries on its own, payments and writes can be
    sent twice; do not.

    Payments and writes must use a connection that no earlier request has used. By default this
    class owns a separate pool for them, built with ``max_keepalive_connections=0`` and without
    HTTP/2, so every payment or write opens a new connection and closes it after the response. The
    ``Connection: close`` header is also sent, but it only takes effect after the response, and
    HTTP/2 drops it, so it is not what provides the guarantee. When you pass ``http_client`` you
    must also pass a dedicated ``payment_http_client`` built the same way (see
    ``selcompy.PAYMENT_LIMITS``, with ``http2=False``). Injected clients must not set default
    ``params`` or ``auth``, because those would change the signed request.
    """

    def __init__(
        self,
        config: Config,
        *,
        http_client: httpx.Client | None = None,
        payment_http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Clock | None = None,
        signer_factory: SignerFactory | None = None,
    ) -> None:
        if transport is not None and (http_client is not None or payment_http_client is not None):
            raise ConfigError("pass transport, or http_client with payment_http_client, not both")
        if http_client is not None:
            if payment_http_client is None or payment_http_client is http_client:
                raise ConfigError(
                    "payments and writes need their own client: pass a dedicated "
                    "payment_http_client (see PAYMENT_LIMITS)"
                )
            check_injected(http_client)
            check_injected(payment_http_client)
        elif payment_http_client is not None:
            raise ConfigError("payment_http_client needs http_client")
        self._core = Core(config, clock, signer_factory)
        self._owns_http = http_client is None
        if http_client is not None:
            self._http = http_client
            self._pay_http = payment_http_client or http_client
        elif transport is not None:
            self._http = httpx.Client(transport=transport, follow_redirects=False)
            self._pay_http = self._http
        else:
            self._http = httpx.Client(
                transport=httpx.HTTPTransport(retries=0, http2=False), follow_redirects=False
            )
            self._pay_http = httpx.Client(
                transport=httpx.HTTPTransport(retries=0, http2=False, limits=PAYMENT_LIMITS),
                follow_redirects=False,
            )

    @property
    def config(self) -> Config:
        return self._core.config

    def __repr__(self) -> str:
        return f"Client(environment={self._core.config.environment.value!r})"

    def close(self) -> None:
        """Close the HTTP clients, if this object created them."""
        if self._owns_http:
            self._http.close()
            if self._pay_http is not self._http:
                self._pay_http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _execute(self, plan: _ops.Plan) -> Result[Any]:
        prepared = self._core.prepare(plan)
        http = self._pay_http if plan.spec.fresh_connection else self._http
        failure: TransportError | None = None
        try:
            response = http.request(
                prepared.method,
                prepared.url,
                headers=prepared.headers,
                content=prepared.content,
                timeout=prepared.timeout,
                follow_redirects=False,
            )
        except Exception as exc:
            # Built here, raised below: raising inside this block would keep the library
            # exception, and the request it carries, as __context__.
            failure = transport_error(plan.spec.name, exc)
        if failure is not None:
            raise failure
        return self._core.finish(plan, response.status_code, response.content)

    def create_order(self, req: CreateOrderRequest) -> Result[OrderCreated]:
        """``checkout.create-order``: an order any enabled method can pay, cards included."""
        return self._execute(_ops.create_order(req))

    def create_order_minimal(self, req: CreateOrderMinimalRequest) -> Result[OrderCreated]:
        """``checkout.create-order-minimal``: wallet push, token or QR."""
        return self._execute(_ops.create_order_minimal(req))

    def wallet_payment(self, req: WalletPaymentRequest) -> Result[None]:
        """``checkout.wallet-payment``: push a USSD prompt. Never retried."""
        return self._execute(_ops.wallet_payment(req))

    def card_payment(self, req: CardPaymentRequest) -> Result[None]:
        """``checkout.card-payment``: charge a stored card. Never retried."""
        return self._execute(_ops.card_payment(req))

    def order_status(self, order_id: str) -> Result[OrderStatusData]:
        """``checkout.order-status``: the source of truth for checkout."""
        return self._execute(_ops.order_status(order_id))

    def cancel_order(self, order_id: str) -> Result[None]:
        """``checkout.cancel-order``."""
        return self._execute(_ops.cancel_order(order_id))

    def list_orders(self, fromdate: date | str, todate: date | str) -> Result[list[OrderSummary]]:
        """``checkout.list-orders``."""
        return self._execute(_ops.list_orders(fromdate, todate))

    def stored_cards(self, gateway_buyer_uuid: str, buyer_userid: str) -> Result[list[StoredCard]]:
        """``checkout.stored-cards``."""
        return self._execute(_ops.stored_cards(gateway_buyer_uuid, buyer_userid))

    def delete_card(self, card_id: str | int, gateway_buyer_uuid: str) -> Result[None]:
        """``checkout.delete-card``."""
        return self._execute(_ops.delete_card(card_id, gateway_buyer_uuid))

    def utility_payment(self, req: UtilityPaymentRequest) -> Result[PaymentData]:
        """``utilitypayment.process``. Never retried."""
        return self._execute(_ops.utility_payment(req))

    def utility_lookup(self, utilitycode: str, utilityref: str, transid: str) -> Result[LookupData]:
        """``utilitypayment.lookup``."""
        return self._execute(_ops.utility_lookup(utilitycode, utilityref, transid))

    def utility_query(self, transid: str) -> Result[PaymentData]:
        """``utilitypayment.query``: the verdict on an earlier payment."""
        return self._execute(_ops.utility_query(transid))

    def wallet_cashin(self, req: WalletCashInRequest) -> Result[PaymentData]:
        """``walletcashin.process``. Never retried."""
        return self._execute(_ops.wallet_cashin(req))

    def wallet_name_lookup(
        self, utilitycode: str, utilityref: str, transid: str
    ) -> Result[LookupData]:
        """``walletcashin.namelookup``."""
        return self._execute(_ops.wallet_name_lookup(utilitycode, utilityref, transid))

    def wallet_cashin_query(self, transid: str) -> Result[PaymentData]:
        """``walletcashin.query``: the verdict on an earlier cash-in."""
        return self._execute(_ops.wallet_cashin_query(transid))

    def vendor_balance(self, transid: str) -> Result[Balance]:
        """``vendor.balance``: float account balance. Moves no money."""
        return self._execute(_ops.vendor_balance(transid))
