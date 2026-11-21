"""Exceptions raised by the client.

No exception carries a raw request: no URL with a query string, no headers, no body. Where an
underlying library error is relevant, only its type name is kept.
"""

from __future__ import annotations

import enum


class SelcomError(Exception):
    """Base class of every error this package raises on purpose."""


class ConfigError(SelcomError):
    """The configuration is incomplete or inconsistent. Raised before anything is sent."""


class ValidationError(SelcomError):
    """A request object is not valid. Raised before signing, so nothing is sent."""


class SigningError(SelcomError, ValueError):
    """The request cannot be signed, so it must not be sent.

    ``code`` is one of the error codes used by the spec vectors, for example ``EMPTY_SECRET`` or
    ``NON_INTEGER_NUMBER``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class TransportClassification(enum.Enum):
    """What is known about a request that produced no HTTP response."""

    NOT_SENT = "NOT_SENT"
    """The request provably never reached the gateway. Sending it again with the same ID is safe."""

    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"
    """The gateway may have acted. Treat as AMBIGUOUS: query, never resend."""


class TransportError(SelcomError):
    """No HTTP response was received."""

    def __init__(
        self,
        classification: TransportClassification,
        operation: str,
        cause_type: str,
    ) -> None:
        super().__init__(f"{operation}: no response ({classification.value}, {cause_type})")
        self.classification = classification
        self.operation = operation
        self.cause_type = cause_type

    @property
    def not_sent(self) -> bool:
        return self.classification is TransportClassification.NOT_SENT

    @property
    def outcome_unknown(self) -> bool:
        return self.classification is TransportClassification.UNKNOWN_OUTCOME


class InvalidResponseError(SelcomError):
    """A response arrived without a verdict, for an operation that does not move money.

    The raw body is kept on ``raw_body`` for audit. It is not part of the message or the repr:
    redact it before logging.
    """

    def __init__(self, operation: str, http_status: int, raw_body: str) -> None:
        super().__init__(f"{operation}: response has no verdict (HTTP {http_status})")
        self.operation = operation
        self.http_status = http_status
        self.raw_body = raw_body

    def __repr__(self) -> str:
        return f"InvalidResponseError(operation={self.operation!r}, http_status={self.http_status})"


class ReplayStoreError(SelcomError):
    """The webhook replay store failed. Respond with a 5xx so that the delivery is repeated."""
