"""Checkout webhooks: verification and payload parsing, as in the spec's ``docs/webhooks.md``.

A webhook is a hint, not proof. Confirm with ``order_status`` before you fulfil anything.

Recommended handler order: ``verify``, then store the delivery, then acknowledge with 200, then
confirm with ``order_status`` in the background. If storing fails, or ``verify`` raises
:class:`~selcompy.errors.ReplayStoreError`, answer with a 5xx so the delivery is repeated.
"""

from __future__ import annotations

import enum
import hmac
import inspect
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Protocol, runtime_checkable

from .errors import ConfigError, ReplayStoreError, SigningError
from .redaction import redact_value
from .results import MAX_AMOUNT_TEXT, try_parse_amount
from .signing import MAX_SAFE_INTEGER, HS256Signer, format_value, parse_timestamp, signing_string

MAX_BODY_BYTES: Final = 1_048_576
MAX_REPLAY_ENTRIES: Final = 100_000
_NUMBER = re.compile(r"(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]{1,4}))?")
_MAX_NUMBER_TEXT = 64


class Verdict(enum.Enum):
    VALID = "VALID"
    """Respond 200, process, then confirm with order status."""
    UNSIGNED = "UNSIGNED"
    """No Digest header. Respond 200, process, confirm with order status."""
    INVALID = "INVALID"
    """Respond 401, do not process."""
    STALE = "STALE"
    """Respond 401, do not process."""
    REPLAY = "REPLAY"
    """Respond 200, do not process: it has been handled."""


@runtime_checkable
class AsyncReplayStore(Protocol):
    """The asynchronous form of :class:`ReplayStore`, for a shared store such as a cache server."""

    async def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool: ...


@runtime_checkable
class ReplayStore(Protocol):
    """Remembers digests for the length of the window.

    ``check_and_record`` must be atomic: it returns ``True`` when the key was already recorded and
    still live, and otherwise records it until ``expires_at`` and returns ``False``. Raise any
    exception when the store fails; :func:`verify` turns it into :class:`ReplayStoreError`.
    """

    def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool: ...


class InMemoryReplayStore:
    """A thread-safe store for one process. Use a shared store when you run several."""

    def __init__(self, max_entries: int = MAX_REPLAY_ENTRIES) -> None:
        self._entries: dict[str, datetime] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def check_and_record(self, key: str, expires_at: datetime, now: datetime) -> bool:
        with self._lock:
            expiry = self._entries.get(key)
            if expiry is not None and expiry > now:
                return True
            if len(self._entries) >= self._max:
                self._entries = {k: v for k, v in self._entries.items() if v > now}
                if len(self._entries) >= self._max:
                    # Dropping a live entry would allow a replay, so fail instead.
                    raise ReplayStoreError("replay store is full")
            self._entries[key] = expires_at
            return False

    def __len__(self) -> int:
        return len(self._entries)


class _Number(str):
    """The text of a JSON number, kept unparsed so that nothing is expanded."""

    __slots__ = ()


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite number")


def _loads(text: str) -> object:
    return json.loads(text, parse_int=_Number, parse_float=_Number, parse_constant=_reject_constant)


def _whole_number(text: str) -> int | None:
    """The value of a JSON number if it is a whole number within +-(2^53 - 1), decided from its
    text alone. Huge exponents, tiny exponents and long digit strings return ``None`` without
    being expanded."""
    if len(text) > _MAX_NUMBER_TEXT:
        return None
    match = _NUMBER.fullmatch(text)
    if match is None:
        return None
    sign, whole, frac, exp = match.groups()
    digits = (whole + (frac or "")).lstrip("0")
    exponent = int(exp or "0") - len(frac or "")
    if not digits:
        return 0
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    if exponent < 0:
        return None
    if len(stripped) + exponent > 16:
        return None
    value: int = int(stripped) * 10**exponent
    if value > MAX_SAFE_INTEGER:
        return None
    return -value if sign else value


def _normalise(value: object) -> str | None:
    """The signed text of a body value, or ``None`` if it cannot be signed."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return format_value(value)
    if isinstance(value, _Number):
        number = _whole_number(str(value))
        return None if number is None else str(number)
    if isinstance(value, str):
        return value
    return None


def _lookup(body: Mapping[str, Any], field_name: str) -> tuple[bool, object]:
    if field_name in body:
        return True, body[field_name]
    node: object = body
    for part in field_name.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _body_text(raw_body: bytes | str) -> str | None:
    """The body as text, or ``None`` when it is over the cap. The cap counts UTF-8 bytes."""
    if len(raw_body) > MAX_BODY_BYTES:  # characters never take fewer bytes than their count
        return None
    if isinstance(raw_body, bytes):
        return raw_body.decode("utf-8")
    if len(raw_body.encode("utf-8")) > MAX_BODY_BYTES:
        return None
    return raw_body


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _verdict(seen: object) -> Verdict:
    """VALID or REPLAY from a store's answer. Anything but a bool is a store failure: an
    un-awaited coroutine, for one, would otherwise count as true."""
    if inspect.iscoroutine(seen):
        seen.close()  # never awaited; silence the warning
    if not isinstance(seen, bool):
        raise ReplayStoreError("the replay store must return a bool")
    return Verdict.REPLAY if seen else Verdict.VALID


class WebhookVerifier:
    """Verifies deliveries against the API secret.

    ``clock`` returns an aware ``datetime`` (default: now, UTC). ``window_seconds`` is the allowed
    distance between the signed timestamp and the clock, and the replay window.
    """

    def __init__(
        self,
        secret: str,
        *,
        window_seconds: int = 300,
        replay_store: ReplayStore | None = None,
        async_replay_store: AsyncReplayStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not secret:
            raise ConfigError("the webhook secret is required")
        if window_seconds <= 0:
            raise ConfigError("window_seconds must be positive")
        self._signer = HS256Signer(secret)
        self._window = timedelta(seconds=window_seconds)
        self.replay_store: ReplayStore = (
            InMemoryReplayStore() if replay_store is None else replay_store
        )
        self.async_replay_store = async_replay_store
        if inspect.iscoroutinefunction(self.replay_store.check_and_record):
            raise ConfigError(
                "replay_store.check_and_record is async: pass it as async_replay_store "
                "and use averify"
            )
        if async_replay_store is not None and not inspect.iscoroutinefunction(
            async_replay_store.check_and_record
        ):
            raise ConfigError("async_replay_store.check_and_record must be an async function")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._now()  # a clock without a time zone is a setup mistake: fail here, not per delivery

    def __repr__(self) -> str:
        return f"WebhookVerifier(window_seconds={int(self._window.total_seconds())})"

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ConfigError("the webhook clock must return a timezone-aware datetime")
        return now

    def verify(self, headers: Mapping[str, str], raw_body: bytes | str) -> Verdict:
        """The verdict for one delivery.

        Never raises on bad input: every malformed delivery is ``INVALID``. The exceptions are
        :class:`ReplayStoreError` when the replay store fails (answer with a 5xx) and
        :class:`ConfigError` when the clock is not timezone-aware (a setup mistake).
        """
        now = self._now()
        checked = self._safe_check(headers, raw_body, now)
        if not isinstance(checked, tuple):
            return checked
        digest, timestamp = checked
        expires_at = max(now, timestamp) + self._window
        try:
            seen = self.replay_store.check_and_record(digest, expires_at, now)
        except ReplayStoreError:
            raise
        except Exception:
            raise ReplayStoreError("the replay store failed") from None
        return _verdict(seen)

    async def averify(self, headers: Mapping[str, str], raw_body: bytes | str) -> Verdict:
        """Like :meth:`verify`, for async handlers. Uses ``async_replay_store`` when one was
        given, and the synchronous ``replay_store`` otherwise."""
        now = self._now()
        checked = self._safe_check(headers, raw_body, now)
        if not isinstance(checked, tuple):
            return checked
        digest, timestamp = checked
        expires_at = max(now, timestamp) + self._window
        try:
            if self.async_replay_store is not None:
                seen = await self.async_replay_store.check_and_record(digest, expires_at, now)
            else:
                seen = self.replay_store.check_and_record(digest, expires_at, now)
        except ReplayStoreError:
            raise
        except Exception:
            raise ReplayStoreError("the replay store failed") from None
        return _verdict(seen)

    def _safe_check(
        self, headers: Mapping[str, str], raw_body: bytes | str, now: datetime
    ) -> Verdict | tuple[str, datetime]:
        try:
            return self._check(headers, raw_body, now)
        except Exception:
            return Verdict.INVALID

    def _check(
        self, headers: Mapping[str, str], raw_body: bytes | str, now: datetime
    ) -> Verdict | tuple[str, datetime]:
        digest = _header(headers, "Digest")
        if digest is None:
            return Verdict.UNSIGNED
        if not digest or _header(headers, "Digest-Method") != "HS256":
            return Verdict.INVALID
        timestamp_text = _header(headers, "Timestamp") or ""
        fields_header = _header(headers, "Signed-Fields") or ""
        try:
            timestamp = parse_timestamp(timestamp_text)
        except SigningError:
            return Verdict.INVALID
        text = _body_text(raw_body)
        if text is None:
            return Verdict.INVALID
        body = _loads(text)
        if not isinstance(body, dict) or not fields_header:
            return Verdict.INVALID
        names = fields_header.split(",")
        if "order_id" not in names or "transid" not in names:
            return Verdict.INVALID
        flat: list[tuple[str, str]] = []
        for name in names:
            found, value = _lookup(body, name)
            if not found or "&" in name:
                return Verdict.INVALID
            normalised = _normalise(value)
            if normalised is None or "&" in normalised:
                return Verdict.INVALID
            flat.append((name, normalised))
        expected = self._signer.sign(signing_string(timestamp_text, flat))
        if not hmac.compare_digest(expected.encode("utf-8"), digest.encode("utf-8")):
            return Verdict.INVALID
        if abs(now - timestamp) > self._window:
            return Verdict.STALE
        return digest, timestamp


@dataclass(frozen=True, repr=False)
class WebhookPayload:
    """A delivery's fields. Every field is optional and nothing here is verified.

    Only the fields named in ``Signed-Fields`` are covered by a ``VALID`` verdict; ``amount`` is
    not one of them. Take the amount from ``order_status``.
    """

    order_id: str | None = None
    transid: str | None = None
    reference: str | None = None
    result: str | None = None
    resultcode: str | None = None
    payment_status: str | None = None
    channel: str | None = None
    phone: str | None = None
    amount: str | None = None
    """The amount text as received."""
    amount_tzs: int | None = None
    """Whole shillings, or ``None`` ("unparsed") when the text is not a whole-unit amount."""
    extra: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        shown = {
            name: redact_value(name, getattr(self, name))
            for name in ("order_id", "transid", "reference", "resultcode", "payment_status")
        }
        shown["phone"] = redact_value("phone", self.phone)
        body = ", ".join(f"{k}={v!r}" for k, v in shown.items())
        return f"WebhookPayload({body}, amount_tzs={self.amount_tzs!r})"


_KNOWN = (
    "order_id",
    "transid",
    "reference",
    "result",
    "resultcode",
    "payment_status",
    "channel",
    "phone",
    "amount",
)


def _field_text(value: object) -> str | None:
    if isinstance(value, _Number):
        return str(value) if len(value) <= _MAX_NUMBER_TEXT else None
    return value if isinstance(value, str) else None


def parse_webhook(raw_body: bytes | str) -> WebhookPayload | None:
    """Parse a delivery's body. Never raises: returns ``None`` when the body is not a JSON
    object, and ``None`` fields (or ``amount_tzs=None``) for anything it cannot read."""
    try:
        text = _body_text(raw_body)
        if text is None:
            return None
        body = _loads(text)
        if not isinstance(body, dict):
            return None
        values = {name: _field_text(body.get(name)) for name in _KNOWN}
        amount_raw = body.get("amount")
        amount_value = (
            try_parse_amount(str(amount_raw))
            if isinstance(amount_raw, str) and len(amount_raw) <= MAX_AMOUNT_TEXT
            else (_whole_number(str(amount_raw)) if isinstance(amount_raw, _Number) else None)
        )
        if values["phone"] is None:
            values["phone"] = _field_text(body.get("msisdn"))
        extra = {k: v for k, v in body.items() if k not in _KNOWN}
        return WebhookPayload(**values, amount_tzs=amount_value, extra=_plain(extra))
    except Exception:
        return None


def _plain(value: Any) -> Any:
    """Turn unparsed number tokens back into plain strings for the ``extra`` mapping."""
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value
