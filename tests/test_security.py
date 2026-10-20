import pytest

from daeri.security import redact


@pytest.mark.parametrize("value", [
    'password="very-secret-value"', "password='very-secret-value'",
    "Authorization: Bearer very-secret-value", "Bearer very-secret-value",
    "scheme://user:very-secret-value@localhost/db",
])
def test_known_credential_forms_are_removed(value):
    assert "very-secret-value" not in redact(value)
    assert "REDACTED" in redact(value)


def test_ordinary_profile_context_remains_readable():
    assert redact(["UTC 기준", {"purpose": "운영 절차 확인"}]) == ["UTC 기준", {"purpose": "운영 절차 확인"}]
