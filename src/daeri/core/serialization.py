"""Finite UTF-8 JSON validation shared by input and persistence boundaries."""

import json


def validate_storable_json(value) -> bytes:
    """Return canonical JSON bytes; require finite values, string keys and UTF-8."""
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('JSON object keys must be strings')
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return data
