"""Shared test fixtures: a config, sample requests and a call table over every operation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from selcompy import (
    Address,
    CardPaymentRequest,
    Config,
    CreateOrderMinimalRequest,
    CreateOrderRequest,
    Credentials,
    Environment,
    UtilityPaymentRequest,
    WalletCashInRequest,
    WalletPaymentRequest,
)

ADDRESS = Address(
    firstname="Test",
    lastname="Buyer",
    address_1="1 Test Street",
    city="Dar es Salaam",
    state_or_region="Dar es Salaam",
    postcode_or_pobox="11101",
    country="TZ",
    phone="255700000001",
)
CREDENTIALS = Credentials(
    api_key="test-key-0001", api_secret="test-secret-0001", vendor="TILL123", pin="0000"
)
CONFIG = Config(environment=Environment.SANDBOX, credentials=CREDENTIALS)

ORDER = CreateOrderRequest(
    order_id="ORD-1001",
    buyer_email="buyer@example.com",
    buyer_name="Test Buyer",
    buyer_phone="255700000001",
    amount=8000,
    currency="TZS",
    payment_methods="ALL",
    no_of_items=1,
    billing=ADDRESS,
)
MINIMAL = CreateOrderMinimalRequest(
    order_id="ORD-1002",
    buyer_email="buyer@example.com",
    buyer_name="Test Buyer",
    buyer_phone="255700000001",
    amount=8000,
    currency="TZS",
    no_of_items=1,
)
CASHIN = WalletCashInRequest(
    transid="TXN-WCI-0001", utilitycode="CASHIN", utilityref="255700000001", amount=1000
)
UTILITY = UtilityPaymentRequest(
    transid="TXN-UTL-0001", utilitycode="LUKU", utilityref="54100000001", amount=5000
)

# Works with Client (returns a Result) and AsyncClient (returns an awaitable): same call shape.
CALLS: dict[str, Callable[[Any], Any]] = {
    "checkout.create-order": lambda c: c.create_order(ORDER),
    "checkout.create-order-minimal": lambda c: c.create_order_minimal(MINIMAL),
    "checkout.wallet-payment": lambda c: c.wallet_payment(
        WalletPaymentRequest(transid="TXN-1", order_id="ORD-1002", msisdn="255700000001")
    ),
    "checkout.card-payment": lambda c: c.card_payment(
        CardPaymentRequest(
            transid="TXN-2",
            order_id="ORD-1001",
            card_token="CARDTOKEN-TEST-0001",
            buyer_userid="U-1",
            gateway_buyer_uuid="GBU-TEST-0001",
        )
    ),
    "checkout.order-status": lambda c: c.order_status("ORD-1003"),
    "checkout.cancel-order": lambda c: c.cancel_order("ORD-1003"),
    "checkout.list-orders": lambda c: c.list_orders("2026-11-01", "2026-11-21"),
    "checkout.stored-cards": lambda c: c.stored_cards("GBU-TEST-0001", "U-1"),
    "checkout.delete-card": lambda c: c.delete_card(23, "GBU-TEST-0001"),
    "utilitypayment.process": lambda c: c.utility_payment(UTILITY),
    "utilitypayment.lookup": lambda c: c.utility_lookup("LUKU", "54100000001", "TXN-LKP-0001"),
    "utilitypayment.query": lambda c: c.utility_query("TXN-UTL-0001"),
    "walletcashin.process": lambda c: c.wallet_cashin(CASHIN),
    "walletcashin.namelookup": lambda c: c.wallet_name_lookup(
        "CASHIN", "255700000001", "TXN-WCI-0002"
    ),
    "walletcashin.query": lambda c: c.wallet_cashin_query("TXN-WCI-0001"),
    "vendor.balance": lambda c: c.vendor_balance("TXN-BAL-0001"),
}
