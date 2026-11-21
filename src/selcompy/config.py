"""Client configuration: environment, credentials and timeouts."""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import ConfigError
from .masking import NoSecretCopy, mask_key

PRODUCTION_URL = "https://apigw.selcommobile.com"
SANDBOX_URL = "https://apigwtest.selcommobile.com"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class Environment(enum.Enum):
    """The target environment. There is no default: the caller always chooses."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"
    CUSTOM = "custom"
    """A simulator or a proxy. Needs ``Config.base_url``."""


@dataclass(frozen=True, slots=True)
class Credentials(NoSecretCopy):
    """API key, API secret and vendor (till) ID, plus the float PIN for payouts."""

    api_key: str
    api_secret: str = field(repr=False)
    vendor: str
    pin: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in ("api_key", "api_secret", "vendor"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{name} is required")
        if self.pin is not None and (not isinstance(self.pin, str) or not self.pin):
            raise ConfigError("pin must be a non-empty string when given")

    def __repr__(self) -> str:
        return (
            f"Credentials(api_key={mask_key(self.api_key)!r}, api_secret=<hidden>, "
            f"vendor={self.vendor!r}, pin={'<hidden>' if self.pin else None})"
        )


@dataclass(frozen=True, slots=True)
class Timeouts:
    """Timeouts in seconds for one profile."""

    connect: float
    read: float

    def __post_init__(self) -> None:
        for name in ("connect", "read"):
            value = getattr(self, name)
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            if not ok or not math.isfinite(value) or value <= 0:
                raise ConfigError(
                    f"timeout {name} must be a finite number above zero; "
                    "no operation runs without a timeout"
                )


QUERY_TIMEOUTS = Timeouts(connect=10.0, read=10.0)
"""Status queries, order status and the other operations."""

PAYMENT_TIMEOUTS = Timeouts(connect=15.0, read=180.0)
"""Money-moving operations. The long read timeout is deliberate: timing out early does not
stop the payment, it only turns a known answer into an unknown one."""


def _coerce_environment(value: object) -> Environment:
    if isinstance(value, Environment):
        return value
    if isinstance(value, str):
        try:
            return Environment(value.strip().lower())
        except ValueError:
            pass
    raise ConfigError("environment must be 'sandbox', 'production' or 'custom'")


@dataclass(frozen=True, slots=True)
class Config(NoSecretCopy):
    """Everything a client needs. ``environment`` has no default and nothing falls back to one.

    ``credentials`` signs payouts and, unless ``checkout_credentials`` is given, checkout too.
    """

    environment: Environment
    credentials: Credentials
    checkout_credentials: Credentials | None = None
    base_url: str | None = None
    query_timeouts: Timeouts = QUERY_TIMEOUTS
    payment_timeouts: Timeouts = PAYMENT_TIMEOUTS

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", _coerce_environment(self.environment))
        if not isinstance(self.credentials, Credentials):
            raise ConfigError("credentials must be a Credentials object")
        if self.environment is Environment.CUSTOM:
            if not self.base_url:
                raise ConfigError("the custom environment needs base_url")
            parts = urlsplit(self.base_url)
            loopback = (parts.hostname or "") in _LOOPBACK
            if parts.scheme not in ("https", "http") or not parts.hostname:
                raise ConfigError("base_url must be an absolute http(s) URL")
            if parts.scheme == "http" and not loopback:
                raise ConfigError("base_url must use https unless it points to the local machine")
            if parts.query or parts.fragment or parts.username or parts.password:
                raise ConfigError("base_url must not carry a query, fragment or credentials")
        elif self.base_url is not None:
            raise ConfigError("base_url is only allowed with the custom environment")

    @property
    def root_url(self) -> str:
        """The base URL without a trailing slash."""
        if self.environment is Environment.PRODUCTION:
            return PRODUCTION_URL
        if self.environment is Environment.SANDBOX:
            return SANDBOX_URL
        assert self.base_url is not None  # checked in __post_init__
        return self.base_url.rstrip("/")

    def credentials_for(self, checkout: bool) -> Credentials:
        if checkout and self.checkout_credentials is not None:
            return self.checkout_credentials
        return self.credentials
