"""Small helpers for printing secrets safely."""

from __future__ import annotations

HIDDEN = "[REDACTED]"


def mask_key(value: str) -> str:
    """Show only the last 4 characters of an API key. Short values show nothing."""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]
