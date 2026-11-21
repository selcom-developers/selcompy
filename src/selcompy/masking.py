"""Small helpers for printing secrets safely."""

from __future__ import annotations

from typing import Any

HIDDEN = "[REDACTED]"


def mask_key(value: str) -> str:
    """Show only the last 4 characters of an API key. Short values show nothing."""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


class NoSecretCopy:
    """Mixin for objects that hold secrets: they refuse to pickle, and copies are the same object.

    Pickling would write the secret into a file or a queue; copying would only duplicate it.
    """

    __slots__ = ()

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError(f"{type(self).__name__} holds secrets and cannot be pickled")

    def __copy__(self) -> Any:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return self
