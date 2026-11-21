"""selcompy: typed sync and async client for the Selcom API gateway.

Not affiliated with Selcom Paytech Ltd.
"""

from ._version import __version__
from .async_client import AsyncClient
from .client import Client
from .config import (
    PAYMENT_TIMEOUTS,
    QUERY_TIMEOUTS,
    Config,
    Credentials,
    Environment,
    Timeouts,
)
from .errors import (
    ConfigError,
    InvalidResponseError,
    ReplayStoreError,
    SelcomError,
    SigningError,
    TransportClassification,
    TransportError,
    ValidationError,
)
from .models import (
    Address,
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
from .redaction import redact, redact_body, redact_headers, redact_query
from .results import OperationKind, Result, Status, parse_amount
from .signing import HS256Signer, SignedRequest, Signer, sign_request
from .webhooks import (
    InMemoryReplayStore,
    ReplayStore,
    Verdict,
    WebhookPayload,
    WebhookVerifier,
    parse_webhook,
)

__all__ = [
    "PAYMENT_TIMEOUTS",
    "QUERY_TIMEOUTS",
    "Address",
    "AsyncClient",
    "Balance",
    "CardPaymentRequest",
    "Client",
    "Config",
    "ConfigError",
    "CreateOrderMinimalRequest",
    "CreateOrderRequest",
    "Credentials",
    "Environment",
    "HS256Signer",
    "InMemoryReplayStore",
    "InvalidResponseError",
    "LookupData",
    "OperationKind",
    "OrderCreated",
    "OrderStatusData",
    "OrderSummary",
    "PaymentData",
    "ReplayStore",
    "ReplayStoreError",
    "Result",
    "SelcomError",
    "SignedRequest",
    "Signer",
    "SigningError",
    "Status",
    "StoredCard",
    "Timeouts",
    "TransportClassification",
    "TransportError",
    "UtilityPaymentRequest",
    "ValidationError",
    "Verdict",
    "WalletCashInRequest",
    "WalletPaymentRequest",
    "WebhookPayload",
    "WebhookVerifier",
    "__version__",
    "parse_amount",
    "parse_webhook",
    "redact",
    "redact_body",
    "redact_headers",
    "redact_query",
    "sign_request",
]
