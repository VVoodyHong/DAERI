"""Canonical JSON bytes for storage integrity and target fingerprints."""

import json

from daeri.core.errors import StoreError


def _json(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise StoreError("INVALID_INPUT", "A JSON-compatible value is required.") from None
