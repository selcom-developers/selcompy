"""Redaction for logs. Use it for anything you store or print.

Rules (from the spec):

* secrets (``pin``, ``secret``, ``api_secret``, ``Authorization``, ``Digest``,
  ``card_token``) become ``[REDACTED]``;
* phone numbers keep only their last 4 characters: ``********0001``;
* e-mail keeps the first character of the local part and the domain: ``t***@example.com``;
* names keep their first character: ``T***``;
* ``utilityref`` counts as a phone number on ``/v1/walletcashin/`` paths.

Dotted forms such as ``billing.phone`` follow the rule of their last segment.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, quote

from .masking import HIDDEN, mask_key

_SECRETS = frozenset(
    {"pin", "secret", "api_secret", "authorization", "digest", "card_token", "api_secret_key"}
)
_PHONES = frozenset({"msisdn", "phone", "buyer_phone"})
_EMAILS = frozenset({"buyer_email", "email"})
_NAMES = frozenset({"buyer_name", "firstname", "lastname", "name"})
_KEYS = frozenset({"api_key"})
_CASHIN_PREFIX = "/v1/walletcashin/"


def mask_phone(value: str) -> str:
    """All but the last 4 characters replaced by ``*``."""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def mask_email(value: str) -> str:
    local, at, domain = value.partition("@")
    if not at or not local or not domain:
        return HIDDEN
    return f"{local[0]}***@{domain}"


def mask_name(value: str) -> str:
    return value[:1] + "***" if value else ""


def _is_cashin(path: str | None) -> bool:
    return path is not None and path.split("?")[0].startswith(_CASHIN_PREFIX)


def redact_value(name: str, value: object, path: str | None = None) -> object:
    """Redact one scalar by its field name. Unknown fields come back unchanged."""
    if isinstance(value, (Mapping, list, tuple)):
        return redact(value, path)
    last = name.rsplit(".", 1)[-1].lower()
    if value is None:
        return None
    if last in _SECRETS:
        return HIDDEN
    text = str(value)
    if last in _PHONES or (last == "utilityref" and _is_cashin(path)):
        return mask_phone(text)
    if last in _EMAILS:
        return mask_email(text)
    if last in _NAMES:
        return mask_name(text)
    if last in _KEYS:
        return mask_key(text)
    return value


def redact(data: Any, path: str | None = None) -> Any:
    """Redact flat or nested mappings (and lists of them). Returns a new object."""
    if isinstance(data, Mapping):
        return {str(k): redact_value(str(k), v, path) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact(item, path) for item in data]
    return data


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Hide ``Authorization`` and ``Digest`` (any case); other headers are kept."""
    return {k: (HIDDEN if k.lower() in _SECRETS else v) for k, v in headers.items()}


def redact_query(query: str, path: str | None = None) -> str:
    """Redact a query string. Order is kept; values come back percent-encoded."""
    query = query.lstrip("?")
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    return "&".join(
        quote(k, safe="") + "=" + quote(str(redact_value(k, v, path)), safe="") for k, v in pairs
    )


def redact_body(body: str | bytes, path: str | None = None) -> str:
    """Redact a JSON body. Text that is not JSON is replaced entirely, because it cannot be
    redacted field by field."""
    try:
        decoded = json.loads(body)
    except ValueError:
        return "[UNPARSEABLE BODY REDACTED]"
    return json.dumps(redact(decoded, path), ensure_ascii=False, separators=(",", ":"))
