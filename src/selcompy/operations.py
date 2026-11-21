"""The v1 operations: what each one sends, and how its response is read.

This module has no I/O. A :class:`Plan` is everything the sync and async clients need to run one
operation; you can also build one yourself and run it on your own transport.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from .errors import ValidationError
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
    _day,
    _text,
)
from .results import OperationKind, payment_status_to_status, try_parse_amount

Entries = list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class OperationSpec:
    name: str
    method: str
    path: str
    kind: OperationKind
    write: bool = False
    """Create, cancel and delete: no money moves, but they are not idempotent."""
    checkout: bool = False
    """Signed with the checkout credentials when a second set is configured."""
    needs_vendor: bool = False
    needs_pin: bool = False

    @property
    def fresh_connection(self) -> bool:
        """Payments and writes never reuse a kept-alive connection."""
        return self.kind is OperationKind.PAYMENT or self.write


def _spec(name: str, method: str, kind: OperationKind, **flags: bool) -> OperationSpec:
    path = "/v1/" + name.replace(".", "/")
    return OperationSpec(name=name, method=method, path=path, kind=kind, **flags)


_P, _S, _O, _X = (
    OperationKind.PAYMENT,
    OperationKind.STATUS_QUERY,
    OperationKind.ORDER_STATUS,
    OperationKind.OTHER,
)

CREATE_ORDER: Final = _spec(
    "checkout.create-order", "POST", _X, write=True, checkout=True, needs_vendor=True
)
CREATE_ORDER_MINIMAL: Final = _spec(
    "checkout.create-order-minimal", "POST", _X, write=True, checkout=True, needs_vendor=True
)
WALLET_PAYMENT: Final = _spec("checkout.wallet-payment", "POST", _P, checkout=True)
CARD_PAYMENT: Final = _spec("checkout.card-payment", "POST", _P, checkout=True, needs_vendor=True)
ORDER_STATUS: Final = _spec("checkout.order-status", "GET", _O, checkout=True)
CANCEL_ORDER: Final = _spec("checkout.cancel-order", "DELETE", _X, write=True, checkout=True)
LIST_ORDERS: Final = _spec("checkout.list-orders", "GET", _X, checkout=True)
STORED_CARDS: Final = _spec("checkout.stored-cards", "GET", _X, checkout=True)
DELETE_CARD: Final = _spec("checkout.delete-card", "DELETE", _X, write=True, checkout=True)
UTILITY_PAYMENT: Final = _spec(
    "utilitypayment.process", "POST", _P, needs_vendor=True, needs_pin=True
)
UTILITY_LOOKUP: Final = _spec("utilitypayment.lookup", "GET", _X)
UTILITY_QUERY: Final = _spec("utilitypayment.query", "GET", _S)
WALLET_CASHIN: Final = _spec("walletcashin.process", "POST", _P, needs_vendor=True, needs_pin=True)
WALLET_NAME_LOOKUP: Final = _spec("walletcashin.namelookup", "GET", _X)
WALLET_CASHIN_QUERY: Final = _spec("walletcashin.query", "GET", _S)
VENDOR_BALANCE: Final = _spec("vendor.balance", "POST", _X, needs_vendor=True, needs_pin=True)

ALL_OPERATIONS: Final = (
    CREATE_ORDER,
    CREATE_ORDER_MINIMAL,
    WALLET_PAYMENT,
    CARD_PAYMENT,
    ORDER_STATUS,
    CANCEL_ORDER,
    LIST_ORDERS,
    STORED_CARDS,
    DELETE_CARD,
    UTILITY_PAYMENT,
    UTILITY_LOOKUP,
    UTILITY_QUERY,
    WALLET_CASHIN,
    WALLET_NAME_LOOKUP,
    WALLET_CASHIN_QUERY,
    VENDOR_BALANCE,
)


@dataclass(frozen=True, slots=True)
class Plan:
    """One operation, ready to run: the spec, the caller's parameters and the response parser.

    ``params`` does not hold the vendor or the PIN. The client puts them in front, in the order
    the spec tables use.
    """

    spec: OperationSpec
    params: dict[str, Any]
    parse: Callable[[Entries], Any]
    transid: str | None = None
    order_id: str | None = None


def _none(_: Entries) -> None:
    return None


def _first(factory: Callable[[dict[str, Any]], Any]) -> Callable[[Entries], Any]:
    def parse(entries: Entries) -> Any:
        return factory(entries[0]) if entries else None

    return parse


def _all(factory: Callable[[dict[str, Any]], Any]) -> Callable[[Entries], Any]:
    return lambda entries: [factory(e) for e in entries]


def _summary(entry: dict[str, Any]) -> OrderSummary:
    amount = entry.get("amount")
    text = amount if isinstance(amount, str) else None
    status_text = entry.get("payment_status")
    return OrderSummary(
        order_id=_str(entry.get("order_id")),
        creation_date=_str(entry.get("creation_date")),
        amount=text,
        amount_value=try_parse_amount(text),
        payment_status=_str(status_text),
        status=payment_status_to_status(status_text),
    )


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def create_order(req: CreateOrderRequest) -> Plan:
    return Plan(
        CREATE_ORDER, req.to_params(), _first(OrderCreated.from_entry), order_id=req.order_id
    )


def create_order_minimal(req: CreateOrderMinimalRequest) -> Plan:
    return Plan(
        CREATE_ORDER_MINIMAL,
        req.to_params(),
        _first(OrderCreated.from_entry),
        order_id=req.order_id,
    )


def wallet_payment(req: WalletPaymentRequest) -> Plan:
    return Plan(WALLET_PAYMENT, req.to_params(), _none, transid=req.transid, order_id=req.order_id)


def card_payment(req: CardPaymentRequest) -> Plan:
    return Plan(CARD_PAYMENT, req.to_params(), _none, transid=req.transid, order_id=req.order_id)


def order_status(order_id: str) -> Plan:
    return Plan(
        ORDER_STATUS,
        {"order_id": _text("order_id", order_id)},
        _first(OrderStatusData.from_entry),
        order_id=order_id,
    )


def cancel_order(order_id: str) -> Plan:
    return Plan(CANCEL_ORDER, {"order_id": _text("order_id", order_id)}, _none, order_id=order_id)


def list_orders(fromdate: date | str, todate: date | str) -> Plan:
    params = {"fromdate": _day("fromdate", fromdate), "todate": _day("todate", todate)}
    return Plan(LIST_ORDERS, params, _all(_summary))


def stored_cards(gateway_buyer_uuid: str, buyer_userid: str) -> Plan:
    params = {
        "gateway_buyer_uuid": _text("gateway_buyer_uuid", gateway_buyer_uuid),
        "buyer_userid": _text("buyer_userid", buyer_userid),
    }
    return Plan(STORED_CARDS, params, _all(StoredCard.from_entry))


def delete_card(card_id: str | int, gateway_buyer_uuid: str) -> Plan:
    if isinstance(card_id, bool) or not isinstance(card_id, (str, int)) or card_id == "":
        raise ValidationError("card_id must be a non-empty string or an integer")
    params = {
        "id": str(card_id),
        "gateway_buyer_uuid": _text("gateway_buyer_uuid", gateway_buyer_uuid),
    }
    return Plan(DELETE_CARD, params, _none)


def utility_payment(req: UtilityPaymentRequest) -> Plan:
    return Plan(
        UTILITY_PAYMENT, req.to_params(), _first(PaymentData.from_entry), transid=req.transid
    )


def utility_lookup(utilitycode: str, utilityref: str, transid: str) -> Plan:
    params = {
        "utilitycode": _text("utilitycode", utilitycode),
        "utilityref": _text("utilityref", utilityref),
        "transid": _text("transid", transid),
    }
    return Plan(UTILITY_LOOKUP, params, _first(LookupData.from_entry), transid=transid)


def utility_query(transid: str) -> Plan:
    return Plan(
        UTILITY_QUERY,
        {"transid": _text("transid", transid)},
        _first(PaymentData.from_entry),
        transid=transid,
    )


def wallet_cashin(req: WalletCashInRequest) -> Plan:
    return Plan(WALLET_CASHIN, req.to_params(), _first(PaymentData.from_entry), transid=req.transid)


def wallet_name_lookup(utilitycode: str, utilityref: str, transid: str) -> Plan:
    params = {
        "utilitycode": _text("utilitycode", utilitycode),
        "utilityref": _text("utilityref", utilityref),
        "transid": _text("transid", transid),
    }
    return Plan(WALLET_NAME_LOOKUP, params, _first(LookupData.from_entry), transid=transid)


def wallet_cashin_query(transid: str) -> Plan:
    return Plan(
        WALLET_CASHIN_QUERY,
        {"transid": _text("transid", transid)},
        _first(PaymentData.from_entry),
        transid=transid,
    )


def vendor_balance(transid: str) -> Plan:
    return Plan(
        VENDOR_BALANCE,
        {"transid": _text("transid", transid)},
        _first(Balance.from_entry),
        transid=transid,
    )
