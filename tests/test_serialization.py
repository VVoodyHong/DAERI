"""Stored JSON must round-trip through UTF-8, including escaped control characters."""

import pytest

from daeri.core.serialization import validate_storable_json


def test_json_escaped_nul_is_storable_without_a_postgres_text_restriction():
    validate_storable_json({'message': 'before\x00after'})


@pytest.mark.parametrize('value', [float('nan'), float('inf'), '\ud800', {'\udfff': 'value'},
                                  {1: 'value'}, {'nested': {2: 'value'}}])
def test_invalid_json_values_keys_or_utf8_are_rejected(value):
    with pytest.raises((ValueError, UnicodeError)):
        validate_storable_json(value)
