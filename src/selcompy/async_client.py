"""The asynchronous client."""

from __future__ import annotations

from datetime import date
from types import TracebackType
from typing import Any

import httpx

from . import operations as _ops
from ._core import Clock, Core, SignerFactory, transport_error
from .config import Config
from .errors import ConfigError
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

    The transport is replaceable. Pass your own ``httpx.AsyncClient`` (proxy, logging, hooks) as
    ``http_client``, or only a custom transport as ``transport`` (a fake in tests). A client you
    pass in is yours to close.

    Nothing is retried, ever: not by this class, and the default transport is built with
    ``retries=0``. If you inject a client or transport that retries on its own, payments and
    writes can be sent twice; do not. Payments and writes send ``Connection: close`` so that they
    use a fresh connection.
    """

    def __init__(
        self,
        config: Config,
        *,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        signer_factory: SignerFactory | None = None,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ConfigError("pass http_client or transport, not both")
        self._core = Core(config, clock, signer_factory)
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0), follow_redirects=False
        )

    @property
    def config(self) -> Config:
        return self._core.config

    def __repr__(self) -> str:
        return f"AsyncClient(environment={self._core.config.environment.value!r})"

    async def aclose(self) -> None:
        """Close the HTTP client, if this object created it."""
        if self._owns_http:
            await self._http.aclose()

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
        try:
            response = await self._http.request(
                prepared.method,
                prepared.url,
                headers=prepared.headers,
                content=prepared.content,
                timeout=prepared.timeout,
                follow_redirects=False,
            )
        except Exception as exc:
            raise transport_error(plan.spec.name, exc) from None
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
