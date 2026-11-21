"""Typed request and response objects.

Request objects hold only what the caller decides. The vendor ID and the float PIN come from the
configuration, and the client adds them. Nothing here prints personal data or secrets: ``repr``
goes through :mod:`selcompy.redaction`.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar, Final
from urllib.parse import urlsplit

from .errors import ValidationError
from .redaction import redact_value
from .results import Status, parse_amount, try_parse_amount

MAX_SAFE_INTEGER: Final = 2**53 - 1
CURRENCIES: Final = ("TZS", "USD")
PAYMENT_METHODS: Final = ("ALL", "MASTERPASS", "CARD", "MOBILEMONEYPULL")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_COLOUR = re.compile(r"#[0-9A-Fa-f]{6}")


class _RedactedRepr:
    """Mixin: ``repr`` with personal data and secrets masked."""

    _redact_path: ClassVar[str | None] = None

    def __repr__(self) -> str:
        parts = []
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            if not f.repr:
                continue
            value = redact_value(f.name, getattr(self, f.name), self._redact_path)
            parts.append(f"{f.name}={value!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _opt_text(name: str, value: object) -> str | None:
    return None if value is None else _text(name, value)


def _amount(name: str, value: object) -> int:
    """A whole, positive amount. Floats are refused: money is never a binary float."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValidationError(f"{name} must be an int or a whole Decimal")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ValidationError(f"{name} must be a whole number")
        value = int(value)
    if not 0 < value <= MAX_SAFE_INTEGER:
        raise ValidationError(f"{name} must be positive and at most {MAX_SAFE_INTEGER}")
    return value


def _currency(value: object) -> str:
    text = str(value).strip().upper() if isinstance(value, str) else ""
    if text not in CURRENCIES:
        raise ValidationError(f"currency must be one of {', '.join(CURRENCIES)}")
    return text


def _url64(name: str, url: str | None) -> str | None:
    """Plain URL in, base64 text out (the form that is signed and sent)."""
    if url is None:
        return None
    parts = urlsplit(_text(name, url))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValidationError(f"{name} must be an absolute http(s) URL")
    return base64.b64encode(url.encode("utf-8")).decode("ascii")


def _methods(value: str | Sequence[str]) -> str:
    items = value.split(",") if isinstance(value, str) else list(value)
    items = [i.strip().upper() for i in items]
    if not items or any(i not in PAYMENT_METHODS for i in items):
        raise ValidationError(f"payment_methods must come from {', '.join(PAYMENT_METHODS)}")
    return ",".join(items)


def _day(name: str, value: date | str) -> str:
    text = value.isoformat() if isinstance(value, date) else value
    if not isinstance(text, str) or _DATE.fullmatch(text) is None:
        raise ValidationError(f"{name} must be a date (YYYY-MM-DD)")
    return text


def _colour(name: str, value: str | None) -> str | None:
    if value is not None and _COLOUR.fullmatch(value) is None:
        raise ValidationError(f"{name} must look like #FF0012")
    return value


def _put(params: dict[str, Any], key: str, value: object) -> None:
    if value is not None:
        params[key] = value


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False, kw_only=True)
class Address(_RedactedRepr):
    """Billing or shipping details. Sent as ``billing.*`` or ``shipping.*`` keys."""

    firstname: str
    lastname: str
    address_1: str
    city: str
    state_or_region: str
    postcode_or_pobox: str
    country: str
    phone: str
    address_2: str | None = None

    def to_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "firstname": _text("firstname", self.firstname),
            "lastname": _text("lastname", self.lastname),
            "address_1": _text("address_1", self.address_1),
        }
        _put(params, "address_2", self.address_2)
        for name in ("city", "state_or_region", "postcode_or_pobox", "country", "phone"):
            params[name] = _text(name, getattr(self, name))
        if len(params["country"]) != 2:
            raise ValidationError("country must be an ISO 3166-1 alpha-2 code")
        return params


@dataclass(frozen=True, repr=False, kw_only=True)
class CreateOrderRequest(_RedactedRepr):
    """``checkout.create-order``. URLs are given plain; the client base64-encodes them."""

    order_id: str
    buyer_email: str
    buyer_name: str
    buyer_phone: str
    amount: int | Decimal
    currency: str
    payment_methods: str | Sequence[str]
    no_of_items: int
    billing: Address
    buyer_userid: str | None = None
    gateway_buyer_uuid: str | None = None
    redirect_url: str | None = None
    cancel_url: str | None = None
    webhook: str | None = None
    shipping: Address | None = None
    buyer_remarks: str | None = None
    merchant_remarks: str | None = None
    header_colour: str | None = None
    link_colour: str | None = None
    button_colour: str | None = None
    expiry: int | None = None

    def to_params(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "order_id": _text("order_id", self.order_id),
            "buyer_email": _text("buyer_email", self.buyer_email),
            "buyer_name": _text("buyer_name", self.buyer_name),
        }
        _put(p, "buyer_userid", self.buyer_userid)
        p["buyer_phone"] = _text("buyer_phone", self.buyer_phone)
        _put(p, "gateway_buyer_uuid", self.gateway_buyer_uuid)
        p["amount"] = _amount("amount", self.amount)
        p["currency"] = _currency(self.currency)
        p["payment_methods"] = _methods(self.payment_methods)
        _put(p, "redirect_url", _url64("redirect_url", self.redirect_url))
        _put(p, "cancel_url", _url64("cancel_url", self.cancel_url))
        _put(p, "webhook", _url64("webhook", self.webhook))
        p["billing"] = self.billing.to_params()
        if self.shipping is not None:
            p["shipping"] = self.shipping.to_params()
        _put(p, "buyer_remarks", self.buyer_remarks)
        _put(p, "merchant_remarks", self.merchant_remarks)
        p["no_of_items"] = _items(self.no_of_items)
        _put(p, "header_colour", _colour("header_colour", self.header_colour))
        _put(p, "link_colour", _colour("link_colour", self.link_colour))
        _put(p, "button_colour", _colour("button_colour", self.button_colour))
        _put(p, "expiry", _expiry(self.expiry))
        return p


def _items(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("no_of_items must be a positive integer")
    return value


def _expiry(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("expiry must be a positive number of minutes")
    return value


@dataclass(frozen=True, repr=False, kw_only=True)
class CreateOrderMinimalRequest(_RedactedRepr):
    """``checkout.create-order-minimal``: wallet push, token or QR payment (no cards)."""

    order_id: str
    buyer_email: str
    buyer_name: str
    buyer_phone: str
    amount: int | Decimal
    currency: str
    no_of_items: int
    redirect_url: str | None = None
    cancel_url: str | None = None
    webhook: str | None = None
    buyer_remarks: str | None = None
    merchant_remarks: str | None = None
    header_colour: str | None = None
    link_colour: str | None = None
    button_colour: str | None = None
    expiry: int | None = None

    def to_params(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "order_id": _text("order_id", self.order_id),
            "buyer_email": _text("buyer_email", self.buyer_email),
            "buyer_name": _text("buyer_name", self.buyer_name),
            "buyer_phone": _text("buyer_phone", self.buyer_phone),
            "amount": _amount("amount", self.amount),
            "currency": _currency(self.currency),
        }
        _put(p, "redirect_url", _url64("redirect_url", self.redirect_url))
        _put(p, "cancel_url", _url64("cancel_url", self.cancel_url))
        _put(p, "webhook", _url64("webhook", self.webhook))
        _put(p, "buyer_remarks", self.buyer_remarks)
        _put(p, "merchant_remarks", self.merchant_remarks)
        p["no_of_items"] = _items(self.no_of_items)
        _put(p, "header_colour", _colour("header_colour", self.header_colour))
        _put(p, "link_colour", _colour("link_colour", self.link_colour))
        _put(p, "button_colour", _colour("button_colour", self.button_colour))
        _put(p, "expiry", _expiry(self.expiry))
        return p


@dataclass(frozen=True, repr=False, kw_only=True)
class WalletPaymentRequest(_RedactedRepr):
    """``checkout.wallet-payment``: push a USSD prompt for an existing order."""

    transid: str
    order_id: str
    msisdn: str

    def to_params(self) -> dict[str, Any]:
        return {
            "transid": _text("transid", self.transid),
            "order_id": _text("order_id", self.order_id),
            "msisdn": _text("msisdn", self.msisdn),
        }


@dataclass(frozen=True, repr=False, kw_only=True)
class CardPaymentRequest(_RedactedRepr):
    """``checkout.card-payment``: charge a stored card for an existing order."""

    transid: str
    order_id: str
    card_token: str = field(repr=False)
    buyer_userid: str
    gateway_buyer_uuid: str

    def to_params(self) -> dict[str, Any]:
        return {
            "transid": _text("transid", self.transid),
            "order_id": _text("order_id", self.order_id),
            "card_token": _text("card_token", self.card_token),
            "buyer_userid": _text("buyer_userid", self.buyer_userid),
            "gateway_buyer_uuid": _text("gateway_buyer_uuid", self.gateway_buyer_uuid),
        }


@dataclass(frozen=True, repr=False, kw_only=True)
class UtilityPaymentRequest(_RedactedRepr):
    """``utilitypayment.process``. Amounts are whole TZS; there is no currency field."""

    transid: str
    utilitycode: str
    utilityref: str
    amount: int | Decimal
    msisdn: str | None = None

    def to_params(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "transid": _text("transid", self.transid),
            "utilitycode": _text("utilitycode", self.utilitycode),
            "utilityref": _text("utilityref", self.utilityref),
            "amount": _amount("amount", self.amount),
        }
        _put(p, "msisdn", _opt_text("msisdn", self.msisdn))
        return p


@dataclass(frozen=True, repr=False, kw_only=True)
class WalletCashInRequest(_RedactedRepr):
    """``walletcashin.process``. ``utilityref`` is a phone number and is masked when printed."""

    _redact_path: ClassVar[str | None] = "/v1/walletcashin/process"

    transid: str
    utilitycode: str
    utilityref: str
    amount: int | Decimal
    msisdn: str | None = None

    def to_params(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "transid": _text("transid", self.transid),
            "utilitycode": _text("utilitycode", self.utilitycode),
            "utilityref": _text("utilityref", self.utilityref),
            "amount": _amount("amount", self.amount),
        }
        _put(p, "msisdn", _opt_text("msisdn", self.msisdn))
        return p


# ---------------------------------------------------------------------------
# Response values
# ---------------------------------------------------------------------------


def _s(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def decode_base64_text(value: str | None) -> str | None:
    """Decode a base64 text field (UTF-8). ``None`` when it is not valid base64 text."""
    if not value:
        return None
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


@dataclass(frozen=True, repr=False)
class OrderCreated(_RedactedRepr):
    gateway_buyer_uuid: str | None
    payment_token: str | None
    qr: str | None
    payment_gateway_url: str | None
    """Decoded from base64. ``None`` when the raw value is missing or not valid base64."""
    payment_gateway_url_raw: str | None

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> OrderCreated:
        raw = _s(entry.get("payment_gateway_url"))
        return cls(
            gateway_buyer_uuid=_s(entry.get("gateway_buyer_uuid")),
            payment_token=_s(entry.get("payment_token")),
            qr=_s(entry.get("qr")),
            payment_gateway_url=decode_base64_text(raw),
            payment_gateway_url_raw=raw,
        )


@dataclass(frozen=True, repr=False)
class OrderStatusData(_RedactedRepr):
    order_id: str | None
    creation_date: str | None
    amount: str | None
    """The amount text as the gateway sent it."""
    amount_value: int | None
    """Whole units, or ``None`` when the text is not a whole-unit amount."""
    payment_status: str | None
    transid: str | None
    channel: str | None
    reference: str | None
    phone: str | None
    """The payer's number (the gateway sends ``phone`` or ``msisdn``)."""

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> OrderStatusData:
        amount = _s(entry.get("amount"))
        return cls(
            order_id=_s(entry.get("order_id")),
            creation_date=_s(entry.get("creation_date")),
            amount=amount,
            amount_value=try_parse_amount(amount),
            payment_status=_s(entry.get("payment_status")),
            transid=_s(entry.get("transid")),
            channel=_s(entry.get("channel")),
            reference=_s(entry.get("reference")),
            phone=_s(entry.get("phone")) or _s(entry.get("msisdn")),
        )


@dataclass(frozen=True, repr=False)
class OrderSummary(_RedactedRepr):
    """One entry of ``checkout.list-orders``, with its own typed status."""

    order_id: str | None
    creation_date: str | None
    amount: str | None
    amount_value: int | None
    payment_status: str | None
    status: Status


@dataclass(frozen=True, repr=False)
class StoredCard(_RedactedRepr):
    id: str | None
    masked_card: str | None
    creation_date: str | None
    card_token: str | None = field(repr=False)
    name: str | None
    card_type: str | None
    """``001`` Visa, ``002`` Mastercard."""

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> StoredCard:
        return cls(
            id=_s(entry.get("id")),
            masked_card=_s(entry.get("masked_card")),
            creation_date=_s(entry.get("creation_date")),
            card_token=_s(entry.get("card_token")),
            name=_s(entry.get("name")),
            card_type=_s(entry.get("card_type")),
        )


@dataclass(frozen=True, repr=False)
class LookupData(_RedactedRepr):
    """``utilitypayment.lookup`` and ``walletcashin.namelookup``."""

    name: str | None
    amount: str | None = None
    institution: str | None = None
    type: str | None = None
    desc: str | None = None

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> LookupData:
        return cls(
            name=_s(entry.get("name")),
            amount=_s(entry.get("amount")),
            institution=_s(entry.get("institution")),
            type=_s(entry.get("type")),
            desc=_s(entry.get("desc")),
        )


@dataclass(frozen=True, repr=False)
class PaymentData(_RedactedRepr):
    """Receipt data of a payout or its status query, when the gateway sends it."""

    receipt: str | None

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> PaymentData:
        return cls(receipt=_s(entry.get("receipt")))


@dataclass(frozen=True, repr=False)
class Balance(_RedactedRepr):
    """``vendor.balance``."""

    balance: int | None
    """Whole shillings, or ``None`` when the text is not a whole-unit amount."""
    balance_text: str | None

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> Balance:
        text = _s(entry.get("balance"))
        return cls(balance=try_parse_amount(text), balance_text=text)


__all__ = [
    "Address",
    "Balance",
    "CardPaymentRequest",
    "CreateOrderMinimalRequest",
    "CreateOrderRequest",
    "LookupData",
    "OrderCreated",
    "OrderStatusData",
    "OrderSummary",
    "PaymentData",
    "StoredCard",
    "UtilityPaymentRequest",
    "WalletCashInRequest",
    "WalletPaymentRequest",
    "decode_base64_text",
    "parse_amount",
]
