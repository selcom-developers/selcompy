from __future__ import annotations

import json
from typing import Any

import pytest

from selcompy import HS256Signer, SigningError, sign_request

from .conftest import SIGNING

CASES = SIGNING["cases"]
REJECTS = SIGNING["rejects"]


def test_vector_counts_are_what_the_spec_ships() -> None:
    assert len(CASES) == 20
    assert len(REJECTS) == 11


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_signing_case(case: dict[str, Any]) -> None:
    expected = case["expected"]
    signed = sign_request(
        case["method"],
        case["params"],
        case["timestamp"],
        case["api_key"],
        HS256Signer(case["secret"]),
    )
    assert [list(pair) for pair in signed.flattened] == expected["flattened"]
    assert signed.signed_fields == expected["signed_fields"]
    assert signed.signing_string == expected["signing_string"]
    assert signed.digest == expected["digest"]
    assert signed.authorization == expected["authorization"]
    assert signed.digest_method == expected["digest_method"]
    assert signed.headers["Timestamp"] == case["timestamp"]
    if case["method"] in ("GET", "DELETE"):
        assert signed.query == expected["query"]
        assert signed.body is None
    else:
        assert signed.body is not None
        ours = json.loads(signed.body)
        theirs = json.loads(expected["body"])
        # same keys, same order, same values and same JSON types
        assert [(k, v, type(v)) for k, v in ours.items()] == [
            (k, v, type(v)) for k, v in theirs.items()
        ]
        assert signed.query is None


@pytest.mark.parametrize("reject", REJECTS, ids=[r["id"] for r in REJECTS])
def test_signing_reject(reject: dict[str, Any]) -> None:
    with pytest.raises(SigningError) as raised:
        sign_request(
            reject["method"],
            reject["params"],
            reject["timestamp"],
            reject["api_key"],
            HS256Signer(reject["secret"]),
        )
    assert raised.value.code == reject["error"]
