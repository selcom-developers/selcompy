from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from selcompy import Status, ValidationError, parse_amount
from selcompy.operations import ALL_OPERATIONS, list_orders
from selcompy.results import OperationKind, classify, normalise_resultcode
from selcompy.signing import format_timestamp, parse_timestamp

from .helpers import MINIMAL, ORDER


@pytest.mark.parametrize("amount", [80.5, 8000.0, True, "8000", 0, -1, Decimal("1.5"), 2**60])
def test_bad_amounts_are_refused(amount: object) -> None:
    with pytest.raises(ValidationError):
        replace(MINIMAL, amount=amount).to_params()  # type: ignore[arg-type]


def test_whole_decimal_amount_is_accepted_as_an_int() -> None:
    assert replace(MINIMAL, amount=Decimal("8000")).to_params()["amount"] == 8000


@pytest.mark.parametrize("currency", ["EUR", "", "tz"])
def test_currency_is_explicit_and_checked(currency: str) -> None:
    with pytest.raises(ValidationError):
        replace(MINIMAL, currency=currency).to_params()
    assert replace(MINIMAL, currency="usd").to_params()["currency"] == "USD"


def test_payment_methods_validation_and_join() -> None:
    assert (
        replace(ORDER, payment_methods=["card", "MOBILEMONEYPULL"]).to_params()["payment_methods"]
        == "CARD,MOBILEMONEYPULL"
    )
    with pytest.raises(ValidationError):
        replace(ORDER, payment_methods="CRYPTO").to_params()


def test_urls_must_be_absolute_http() -> None:
    with pytest.raises(ValidationError):
        replace(MINIMAL, webhook="javascript:alert(1)").to_params()


def test_no_placeholders_for_missing_buyer_data() -> None:
    with pytest.raises(ValidationError):
        replace(MINIMAL, buyer_email="").to_params()


def test_dates_accept_date_objects() -> None:
    plan = list_orders(date(2026, 11, 1), "2026-11-21")
    assert plan.params == {"fromdate": "2026-11-01", "todate": "2026-11-21"}


def test_operation_table_matches_the_spec() -> None:
    assert len(ALL_OPERATIONS) == 16
    by_name = {s.name: s for s in ALL_OPERATIONS}
    assert by_name["checkout.cancel-order"].method == "DELETE"
    assert by_name["vendor.balance"].method == "POST"
    assert by_name["walletcashin.query"].kind is OperationKind.STATUS_QUERY
    assert by_name["checkout.order-status"].kind is OperationKind.ORDER_STATUS
    assert by_name["utilitypayment.process"].path == "/v1/utilitypayment/process"
    writes = {s.name for s in ALL_OPERATIONS if s.write}
    assert writes == {
        "checkout.create-order",
        "checkout.create-order-minimal",
        "checkout.cancel-order",
        "checkout.delete-card",
    }


def test_resultcode_normalisation() -> None:
    assert normalise_resultcode(" 000 ") == "000"
    assert normalise_resultcode(0) == "000"
    assert normalise_resultcode(111) == "111"
    assert normalise_resultcode(True) is None
    assert normalise_resultcode("  ") is None
    assert normalise_resultcode(None) is None


def test_classification_rules() -> None:
    k = OperationKind
    assert classify(k.PAYMENT, None) == (Status.AMBIGUOUS, None)
    assert classify(k.OTHER, {}) == (None, None)
    assert classify(k.STATUS_QUERY, {"resultcode": "404"}) == (Status.NOT_FOUND, "404")
    assert classify(k.OTHER, {"resultcode": "404"}) == (Status.FAILED, "404")
    assert classify(k.ORDER_STATUS, {"resultcode": "500"}) == (Status.AMBIGUOUS, "500")
    assert classify(k.ORDER_STATUS, {"resultcode": "000", "data": []}) == (Status.NOT_FOUND, "000")
    assert classify(k.ORDER_STATUS, {"resultcode": "000"}) == (Status.AMBIGUOUS, "000")
    assert classify(k.PAYMENT, {"resultcode": "111", "result": "SUCCESS"})[0] is Status.PENDING
    assert classify(k.PAYMENT, {"resultcode": "927"})[0] is Status.PENDING
    assert classify(k.PAYMENT, {"resultcode": "000", "result": ""})[0] is Status.SUCCESS
    assert all(s.is_final for s in (Status.SUCCESS, Status.FAILED, Status.NOT_FOUND))
    assert not Status.PENDING.is_final
    assert not Status.AMBIGUOUS.is_final


def test_parse_amount_is_strict() -> None:
    assert parse_amount("10,000") == 10000
    assert parse_amount(" 500 ") == 500
    for bad in ("1e3", "-1", "1,00", "10.5", "", "abc", True, 1.5, None, "٣٣"):
        with pytest.raises(ValueError, match=r".+"):
            parse_amount(bad)


def test_timestamps() -> None:
    assert (
        parse_timestamp("2026-11-21T10:30:46+0300").utcoffset()
        == parse_timestamp("2026-11-21T10:30:46+03:00").utcoffset()
    )
    for bad in ("2026-11-21T10:30:46Z", "2026-13-21T10:30:46+03:00", "2026-11-21T10:30:46+03:99"):
        with pytest.raises(Exception, match=r"INVALID_TIMESTAMP"):
            parse_timestamp(bad)
    naive = date(2026, 1, 1)
    del naive
    from datetime import datetime

    with pytest.raises(Exception, match=r"INVALID_TIMESTAMP"):
        format_timestamp(datetime(2026, 1, 1))
