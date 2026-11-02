"""Canonical JSON bytes and finite float32 vector representations for storage."""

import json
import math
import struct

from daeri.core.errors import StoreError


def validated_float32_vector(value, dimensions=None) -> list[float]:
    """Validate numeric vectors against the finite, nonzero float32 index format."""
    if (not isinstance(value, list) or not 1 <= len(value) <= 8192
            or (dimensions is not None and len(value) != dimensions)
            or any(type(item) not in (int, float) for item in value)):
        raise ValueError('Invalid embedding vector')
    try:
        stored = struct.unpack(f'<{len(value)}f', struct.pack(f'<{len(value)}f', *value))
    except (OverflowError, struct.error):
        raise ValueError('Unindexable embedding vector') from None
    if not all(math.isfinite(item) for item in stored) or not any(stored):
        raise ValueError('Unindexable embedding vector')
    return [float(item) for item in value]


def _json(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise StoreError("INVALID_INPUT", "A JSON-compatible value is required.") from None
