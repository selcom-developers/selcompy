"""Result codes and the five typed statuses, as in the spec's ``docs/results.md``."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Status(enum.Enum):
    """The verdict on a request. Every operation returns one of these five."""

    SUCCESS = "SUCCESS"
    PENDING = "PENDING"
    AMBIGUOUS = "AMBIGUOUS"
    FAILED = "FAILED"
    NOT_FOUND = "NOT_FOUND"

    @property
    def is_final(self) -> bool:
        return self in (Status.SUCCESS, Status.FAILED, Status.NOT_FOUND)


class OperationKind(enum.Enum):
    """Decides how a response is classified and which timeouts apply."""

    PAYMENT = "payment"
    STATUS_QUERY = "status-query"
    ORDER_STATUS = "order-status"
    OTHER = "other"


_PAYMENT_STATUS = {
    "COMPLETED": Status.SUCCESS,
    "COMPLETE": Status.SUCCESS,
    "PENDING": Status.PENDING,
    "INPROGRESS": Status.PENDING,
    "CANCELLED": Status.FAILED,
    "CANCELED": Status.FAILED,
    "CANCEL": Status.FAILED,
    "USERCANCELLED": Status.FAILED,
    "USERCANCELED": Status.FAILED,
    "REJECTED": Status.FAILED,
    "FAILED": Status.FAILED,
}


def normalise_resultcode(value: object) -> str | None:
    """Trim a string; format a JSON number as three digits; anything else has no code."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"{value:03d}" if value >= 0 else None
    if isinstance(value, str):
        return value.strip() or None
    return None


def payment_status_to_status(value: object) -> Status:
    """Map a ``payment_status`` text (order status and list entries) to a typed status."""
    if not isinstance(value, str):
        return Status.AMBIGUOUS
    return _PAYMENT_STATUS.get(value.strip().upper(), Status.AMBIGUOUS)


def _order_status_verdict(body: dict[str, Any]) -> Status:
    data = body.get("data")
    if not isinstance(data, list):
        return Status.AMBIGUOUS
    if not data:
        return Status.NOT_FOUND
    entry = data[0]
    if not isinstance(entry, dict):
        return Status.AMBIGUOUS
    return payment_status_to_status(entry.get("payment_status"))


def classify(kind: OperationKind, body: object) -> tuple[Status | None, str | None]:
    """Return ``(status, resultcode)`` for a decoded body.

    ``status`` is ``None`` only for the ``OTHER`` kind when there is no usable result code; the
    caller raises an invalid-response error then.
    """
    code = normalise_resultcode(body.get("resultcode")) if isinstance(body, dict) else None
    if code is None or not isinstance(body, dict):
        return (None if kind is OperationKind.OTHER else Status.AMBIGUOUS), None
    if code == "000":
        return (
            _order_status_verdict(body) if kind is OperationKind.ORDER_STATUS else Status.SUCCESS
        ), code
    if code in ("111", "927"):
        return Status.PENDING, code
    if code == "999":
        return Status.AMBIGUOUS, code
    if code == "404" and kind in (OperationKind.STATUS_QUERY, OperationKind.ORDER_STATUS):
        return Status.NOT_FOUND, code
    if kind is OperationKind.ORDER_STATUS:
        return Status.AMBIGUOUS, code
    return Status.FAILED, code


_AMOUNT = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.0+)?")
MAX_AMOUNT_TEXT = 40


def parse_amount(value: object) -> int:
    """Strict whole-unit amount from a response or callback field.

    Accepts ``"10000"``, ``"10,000"``, ``"10000.00"`` and plain integers. Anything else raises
    ``ValueError``: fractions, signs, exponents, separators in the wrong place, huge values.
    """
    if isinstance(value, bool):
        raise ValueError("a boolean is not an amount")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > MAX_AMOUNT_TEXT or _AMOUNT.fullmatch(text) is None:
            raise ValueError("not a whole-unit amount")
        number = int(text.split(".")[0].replace(",", ""))
    else:
        raise ValueError("not a whole-unit amount")
    if not 0 <= number <= 2**53 - 1:
        raise ValueError("amount out of range")
    return number


def try_parse_amount(value: object) -> int | None:
    """Like :func:`parse_amount`, but ``None`` instead of an error."""
    try:
        return parse_amount(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Result(Generic[T]):
    """What every operation returns.

    ``data`` and ``raw`` can hold personal data and are left out of ``repr``: redact them (see
    :mod:`selcompy.redaction`) before logging.
    """

    operation: str
    status: Status
    http_status: int
    resultcode: str | None = None
    result: str | None = None
    message: str | None = None
    reference: str | None = None
    transid: str | None = None
    order_id: str | None = None
    data: list[dict[str, Any]] = field(default_factory=list, repr=False)
    value: T | None = field(default=None, repr=False)
    """The typed form of ``data`` for this operation, when it has one."""
    raw: str = field(default="", repr=False)

    def is_final(self) -> bool:
        return self.status.is_final

    def __repr__(self) -> str:
        return (
            f"Result(operation={self.operation!r}, status={self.status.name}, "
            f"http_status={self.http_status}, resultcode={self.resultcode!r}, "
            f"reference={self.reference!r}, transid={self.transid!r}, order_id={self.order_id!r})"
        )
