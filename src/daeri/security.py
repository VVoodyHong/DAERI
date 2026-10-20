"""Known credential patterns only; not a general secret or PII detector."""

import re

_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|access_token|refresh_token|api[_-]?key|"
    r"client_secret|secret|authorization)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URI = re.compile(r"(\w+://[^\s:/@]+:)[^\s/@]+(@)")


def redact(value):
    if isinstance(value, str):
        value = _BEARER.sub("Bearer [REDACTED]", value)
        value = _ASSIGNMENT.sub(lambda match: match[1] + "=[REDACTED]", value)
        return _URI.sub(r"\1[REDACTED]\2", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
