"""The synchronous client."""

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


class Client:
    """Typed operations over httpx.

    The transport is replaceable. Pass your own ``httpx.Client`` (proxy, logging, event hooks) as
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
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Clock | None = None,
        signer_factory: SignerFactory | None = None,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ConfigError("pass http_client or transport, not both")
        self._core = Core(config, clock, signer_factory)
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0), follow_redirects=False
        )

    @property
    def config(self) -> Config:
        return self._core.config

    def __repr__(self) -> str:
        return f"Client(environment={self._core.config.environment.value!r})"

    def close(self) -> None:
        """Close the HTTP client, if this object created it."""
        if self._owns_http:
            self._http.close()

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
        try:
            response = self._http.request(
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
