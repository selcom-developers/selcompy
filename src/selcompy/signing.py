"""Request signing, as described in the spec's ``docs/signing.md``.

Everything here is a pure function: no I/O and no clock. The timestamp is an argument.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import quote

from .errors import SigningError
from .masking import HIDDEN, NoSecretCopy

MAX_SAFE_INTEGER: Final = 2**53 - 1
MAX_DEPTH: Final = 8
DAR_ES_SALAAM: Final = timezone(timedelta(hours=3), "Africa/Dar_es_Salaam")

_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})([+-])([0-9]{2}):?([0-9]{2})"
)
_KEY = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")

Scalar = str | int | bool
FlatPairs = tuple[tuple[str, Scalar], ...]


@runtime_checkable
class Signer(Protocol):
    """The digest step. HS256 is implemented; the interface leaves room for RS256."""

    @property
    def method(self) -> str:
        """Sent as ``Digest-Method``."""
        ...

    def sign(self, signing_string: str) -> str:
        """Return the base64 digest of the signing string. Never empty."""
        ...


class HS256Signer(NoSecretCopy):
    """HMAC-SHA256 keyed with the API secret."""

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        if not secret:
            raise SigningError("EMPTY_SECRET", "refusing to sign with an empty secret")
        self._secret = secret.encode("utf-8")

    @property
    def method(self) -> str:
        return "HS256"

    def sign(self, signing_string: str) -> str:
        mac = hmac.new(self._secret, signing_string.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(mac).decode("ascii")

    def __repr__(self) -> str:
        return "HS256Signer(secret=<hidden>)"


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """Everything that goes on the wire for one request."""

    flattened: FlatPairs
    signed_fields: str
    signing_string: str = ""
    digest: str = ""
    authorization: str = ""
    digest_method: str = "HS256"
    timestamp: str = ""
    query: str | None = None
    body: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        """The signing headers. ``Accept`` and ``Content-Type`` are added by the client."""
        return {
            "Authorization": self.authorization,
            "Digest-Method": self.digest_method,
            "Digest": self.digest,
            "Timestamp": self.timestamp,
            "Signed-Fields": self.signed_fields,
        }

    def __repr__(self) -> str:
        return (
            f"SignedRequest(signed_fields={self.signed_fields!r}, timestamp={self.timestamp!r}, "
            f"authorization={HIDDEN!r}, digest={HIDDEN!r})"
        )


def parse_timestamp(timestamp: str) -> datetime:
    """Parse ``yyyy-MM-ddTHH:mm:ss+HH:MM`` (or ``+HHMM``) into an aware datetime."""
    match = _TIMESTAMP.fullmatch(timestamp) if isinstance(timestamp, str) else None
    if match is None:
        raise SigningError(
            "INVALID_TIMESTAMP", "timestamp must be yyyy-MM-ddTHH:mm:ss+HH:MM or +HHMM"
        )
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    offset_hours, offset_minutes = int(match.group(8)), int(match.group(9))
    try:
        if offset_minutes > 59:
            raise ValueError("offset minutes")
        offset = timedelta(hours=offset_hours, minutes=offset_minutes)
        if match.group(7) == "-":
            offset = -offset
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone(offset))
    except ValueError:
        raise SigningError("INVALID_TIMESTAMP", "timestamp is not a real date and time") from None


def format_timestamp(moment: datetime) -> str:
    """Format an aware datetime in the colon form, truncated to whole seconds."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise SigningError("INVALID_TIMESTAMP", "timestamp needs a time zone")
    return moment.replace(microsecond=0).isoformat(timespec="seconds")


def normalise_value(key: str, value: object) -> Scalar:
    """The scalar that is both signed and sent for one field."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        number = int(value)  # a plain int, also for IntEnum and other subclasses
        if abs(number) > MAX_SAFE_INTEGER:
            raise SigningError(
                "NUMBER_OUT_OF_RANGE", f"{key}: send integers above 2^53-1 as strings"
            )
        return number
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise SigningError("NON_INTEGER_NUMBER", f"{key}: the number has a fractional part")
        return normalise_value(key, int(value))
    if isinstance(value, str):
        return str.__str__(value)  # plain text, also for str-based Enums and other subclasses
    raise SigningError("UNSUPPORTED_TYPE", f"{key}: {type(value).__name__} cannot be signed")


def format_value(value: Scalar) -> str:
    """Text of a normalised value inside the signing string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten(params: Mapping[str, Any]) -> FlatPairs:
    """Flatten nested objects into dotted keys, in place, keeping send order."""
    if not isinstance(params, Mapping):
        raise SigningError("UNSUPPORTED_TYPE", "params must be a mapping")
    flat: list[tuple[str, Scalar]] = []
    seen: set[str] = set()

    def walk(prefix: str, node: Mapping[str, Any], depth: int) -> None:
        if depth > MAX_DEPTH:
            raise SigningError("INVALID_KEY", f"{prefix}: nesting is too deep")
        for raw_key, value in node.items():
            if not isinstance(raw_key, str) or raw_key == "":
                raise SigningError("INVALID_KEY", "empty or non-string key")
            key = f"{prefix}.{str.__str__(raw_key)}" if prefix else str.__str__(raw_key)
            if _KEY.fullmatch(key) is None:
                raise SigningError("INVALID_KEY", f"{key!r} has characters outside [A-Za-z0-9_.-]")
            if isinstance(value, Mapping):
                if not value:
                    raise SigningError("EMPTY_OBJECT", f"{key} is an empty object; omit it")
                walk(key, value, depth + 1)
                continue
            if key in seen:
                raise SigningError("DUPLICATE_KEY", f"{key} appears twice after flattening")
            seen.add(key)
            flat.append((key, normalise_value(key, value)))

    walk("", params, 0)
    if not flat:
        raise SigningError("EMPTY_PARAMS", "there is nothing to sign")
    return tuple(flat)


def signing_string(timestamp: str, flat: Iterable[tuple[str, Scalar]]) -> str:
    return "timestamp=" + timestamp + "".join(f"&{k}={format_value(v)}" for k, v in flat)


def authorization_header(api_key: str) -> str:
    if not api_key:
        raise SigningError("EMPTY_API_KEY", "refusing to build Authorization from an empty key")
    return "SELCOM " + base64.b64encode(api_key.encode("utf-8")).decode("ascii")


def encode_query(flat: Iterable[tuple[str, Scalar]]) -> str:
    """RFC 3986 percent-encoding: only ``A-Z a-z 0-9 - . _ ~`` stay as they are."""
    return "&".join(quote(k, safe="") + "=" + quote(format_value(v), safe="") for k, v in flat)


def json_body(flat: Iterable[tuple[str, Scalar]]) -> str:
    """Compact JSON object with the flat keys, in send order."""
    return json.dumps(dict(flat), ensure_ascii=False, separators=(",", ":"))


def sign_request(
    method: str,
    params: Mapping[str, Any],
    timestamp: str,
    api_key: str,
    signer: Signer,
) -> SignedRequest:
    """Sign one request. Raises :class:`SigningError`, and then nothing may be sent."""
    verb = method.upper()
    if verb not in ("GET", "POST", "DELETE"):
        raise SigningError("UNSUPPORTED_METHOD", verb)
    authorization = authorization_header(api_key)
    parse_timestamp(timestamp)
    flat = flatten(params)
    message = signing_string(timestamp, flat)
    digest = signer.sign(message)
    if not digest:
        raise SigningError("EMPTY_DIGEST", "refusing to send an empty Digest")
    fields = ",".join(k for k, _ in flat)
    return SignedRequest(
        flattened=flat,
        signed_fields=fields,
        signing_string=message,
        digest=digest,
        authorization=authorization,
        digest_method=signer.method,
        timestamp=timestamp,
        query=encode_query(flat) if verb in ("GET", "DELETE") else None,
        body=json_body(flat) if verb == "POST" else None,
    )
