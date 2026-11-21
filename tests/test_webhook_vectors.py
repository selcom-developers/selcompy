from __future__ import annotations

from datetime import datetime

from selcompy import InMemoryReplayStore, Verdict, WebhookVerifier, parse_webhook

from .conftest import WEBHOOKS

SAMPLES = WEBHOOKS["samples"]


def test_webhook_samples_in_order_through_one_store() -> None:
    config = WEBHOOKS["verification"]
    now = datetime.fromisoformat(config["now"])
    verifier = WebhookVerifier(
        config["secret"],
        window_seconds=config["window_seconds"],
        replay_store=InMemoryReplayStore(),
        clock=lambda: now,
    )
    assert len(SAMPLES) == 15
    for sample in SAMPLES:
        verdict = verifier.verify(sample["headers"], sample["raw_body"])
        assert verdict.value == sample["expected_verification"], sample["name"]
        payload = parse_webhook(sample["raw_body"])
        assert payload is not None, sample["name"]
        assert payload.amount_tzs == sample["expected_amount_tzs"], sample["name"]


def test_webhook_samples_as_bytes() -> None:
    config = WEBHOOKS["verification"]
    now = datetime.fromisoformat(config["now"])
    verifier = WebhookVerifier(config["secret"], clock=lambda: now)
    sample = SAMPLES[0]
    assert verifier.verify(sample["headers"], sample["raw_body"].encode()) is Verdict.VALID
