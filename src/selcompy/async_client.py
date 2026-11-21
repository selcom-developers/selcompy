"""The asynchronous client."""

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


class AsyncClient:
    """Typed operations over httpx, asynchronous.

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
        http_client: httpx.AsyncClient | None = None,
        payment_http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
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
            self._http = httpx.AsyncClient(transport=transport, follow_redirects=False)
            self._pay_http = self._http
        else:
            self._http = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0, http2=False), follow_redirects=False
            )
            self._pay_http = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0, http2=False, limits=PAYMENT_LIMITS),
                follow_redirects=False,
            )

    @property
    def config(self) -> Config:
        return self._core.config

    def __repr__(self) -> str:
        return f"AsyncClient(environment={self._core.config.environment.value!r})"

    async def aclose(self) -> None:
        """Close the HTTP clients, if this object created them."""
        if self._owns_http:
            await self._http.aclose()
            if self._pay_http is not self._http:
                await self._pay_http.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _execute(self, plan: _ops.Plan) -> Result[Any]:
        prepared = self._core.prepare(plan)
        http = self._pay_http if plan.spec.fresh_connection else self._http
        failure: TransportError | None = None
        try:
            response = await http.request(
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

    async def create_order(self, req: CreateOrderRequest) -> Result[OrderCreated]:
        """``checkout.create-order``: an order any enabled method can pay, cards included."""
        return await self._execute(_ops.create_order(req))

    async def create_order_minimal(self, req: CreateOrderMinimalRequest) -> Result[OrderCreated]:
        """``checkout.create-order-minimal``: wallet push, token or QR."""
        return await self._execute(_ops.create_order_minimal(req))

    async def wallet_payment(self, req: WalletPaymentRequest) -> Result[None]:
        """``checkout.wallet-payment``: push a USSD prompt. Never retried."""
        return await self._execute(_ops.wallet_payment(req))

    async def card_payment(self, req: CardPaymentRequest) -> Result[None]:
        """``checkout.card-payment``: charge a stored card. Never retried."""
        return await self._execute(_ops.card_payment(req))

    async def order_status(self, order_id: str) -> Result[OrderStatusData]:
        """``checkout.order-status``: the source of truth for checkout."""
        return await self._execute(_ops.order_status(order_id))

    async def cancel_order(self, order_id: str) -> Result[None]:
        """``checkout.cancel-order``."""
        return await self._execute(_ops.cancel_order(order_id))

    async def list_orders(
        self, fromdate: date | str, todate: date | str
    ) -> Result[list[OrderSummary]]:
        """``checkout.list-orders``."""
        return await self._execute(_ops.list_orders(fromdate, todate))

    async def stored_cards(
        self, gateway_buyer_uuid: str, buyer_userid: str
    ) -> Result[list[StoredCard]]:
        """``checkout.stored-cards``."""
        return await self._execute(_ops.stored_cards(gateway_buyer_uuid, buyer_userid))

    async def delete_card(self, card_id: str | int, gateway_buyer_uuid: str) -> Result[None]:
        """``checkout.delete-card``."""
        return await self._execute(_ops.delete_card(card_id, gateway_buyer_uuid))

    async def utility_payment(self, req: UtilityPaymentRequest) -> Result[PaymentData]:
        """``utilitypayment.process``. Never retried."""
        return await self._execute(_ops.utility_payment(req))

    async def utility_lookup(
        self, utilitycode: str, utilityref: str, transid: str
    ) -> Result[LookupData]:
        """``utilitypayment.lookup``."""
        return await self._execute(_ops.utility_lookup(utilitycode, utilityref, transid))

    async def utility_query(self, transid: str) -> Result[PaymentData]:
        """``utilitypayment.query``: the verdict on an earlier payment."""
        return await self._execute(_ops.utility_query(transid))

    async def wallet_cashin(self, req: WalletCashInRequest) -> Result[PaymentData]:
        """``walletcashin.process``. Never retried."""
        return await self._execute(_ops.wallet_cashin(req))

    async def wallet_name_lookup(
        self, utilitycode: str, utilityref: str, transid: str
    ) -> Result[LookupData]:
        """``walletcashin.namelookup``."""
        return await self._execute(_ops.wallet_name_lookup(utilitycode, utilityref, transid))

    async def wallet_cashin_query(self, transid: str) -> Result[PaymentData]:
        """``walletcashin.query``: the verdict on an earlier cash-in."""
        return await self._execute(_ops.wallet_cashin_query(transid))

    async def vendor_balance(self, transid: str) -> Result[Balance]:
        """``vendor.balance``: float account balance. Moves no money."""
        return await self._execute(_ops.vendor_balance(transid))
