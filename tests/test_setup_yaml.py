import math

import pytest

from daeri.storage import StoreError


def codec():
    from daeri import setup_yaml
    return setup_yaml


def document(tmp_path, text):
    path = tmp_path / 'setup.yaml'
    path.write_text(text, encoding='utf-8')
    return path


def test_setup_yaml_round_trip_preserves_types_and_korean(tmp_path):
    value = {'profile': 'service-prod', 'resources': {
        'primary-db': {'adapter': 'db-postgres', 'inputs': {
            'host': 'db.example.test', 'port': 5432, 'tables': ['*'],
            'enabled': True, 'blank': None, 'description': '운영 조회',
            'account': '123456789012', 'ratio': 0.5}}}}
    encoded = codec().write_yaml(value)
    assert '운영 조회' in encoded
    assert encoded.startswith('profile: service-prod\nresources:')
    assert codec().read_yaml(document(tmp_path, encoded)) == value


def test_setup_yaml_uses_json_compatible_scalar_types(tmp_path):
    value = codec().read_yaml(document(tmp_path, '''profile: service
inputs:
  enabled: true
  disabled: false
  unset: null
  exponent: 1e3
  ordinary_word: yes
  switch_name: on
  iso_date: 2026-01-02
  literal_reference: ${DATABASE_HOST}
'''))
    assert value['inputs'] == {
        'enabled': True, 'disabled': False, 'unset': None, 'exponent': 1000.0,
        'ordinary_word': 'yes', 'switch_name': 'on', 'iso_date': '2026-01-02',
        'literal_reference': '${DATABASE_HOST}'}


def test_setup_yaml_accepts_comments_and_caller_owned_secret_validation(tmp_path):
    value = codec().read_yaml(document(tmp_path, '# public comment\nprofile: service\npassword: caller-validates\n'))
    assert value == {'profile': 'service', 'password': 'caller-validates'}


def test_setup_yaml_can_parse_the_exact_bytes_used_for_confirmation():
    assert codec().parse_yaml('profile: 운영\n'.encode('utf-8')) == {'profile': '운영'}


@pytest.mark.parametrize('content', [b'profile: \xff', b'key: 1\nkey: 2\n',
                                     b'#' * (2 * 1024 * 1024 + 1), 'profile: service'])
def test_setup_yaml_bytes_parser_uses_the_same_bounds_and_validation(content):
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        codec().parse_yaml(content)


@pytest.mark.parametrize('text', [
    'profile: one\nprofile: secret-input\n',
    'resources: {same: 1, same: secret-input}\n',
    'profile: &secret-input service\n',
    'profile: *secret-input\n',
    'profile: !!str secret-input\n',
    'profile: !custom secret-input\n',
    'resources: {<<: {secret-input: value}}\n',
    'resources: {"<<": {secret-input: value}}\n',
    'profile: one\n---\nprofile: secret-input\n',
    'profile: .nan\n', 'profile: .inf\n', 'profile: -.Inf\n',
    'profile: 1e999\n',
    '1: secret-input\n', 'false: secret-input\n', 'null: secret-input\n',
    '? [secret-input]\n: value\n',
    '[secret-input]\n', 'secret-input\n', '',
    'profile: [secret-input\n',
    '%TAG !foo! tag:example.test,2026:\n---\nprofile: secret-input\n',
])
def test_setup_yaml_rejects_unsafe_or_ambiguous_documents_without_echoing_values(tmp_path, text):
    with pytest.raises(StoreError, match='INVALID_INPUT') as failure:
        codec().read_yaml(document(tmp_path, text))
    assert 'secret-input' not in str(failure.value)


def test_setup_yaml_syntax_error_has_safe_location(tmp_path):
    with pytest.raises(StoreError) as failure:
        codec().read_yaml(document(tmp_path, 'profile: [private-input\n'))
    assert '행' in str(failure.value)
    assert '열' in str(failure.value)
    assert 'private-input' not in str(failure.value)


def test_setup_yaml_rejects_oversized_and_non_utf8_files(tmp_path):
    oversized = tmp_path / 'oversized.yaml'
    oversized.write_bytes(b'#' * (2 * 1024 * 1024 + 1))
    invalid_encoding = tmp_path / 'encoding.yaml'
    invalid_encoding.write_bytes(b'profile: \xff\n')
    for path in (oversized, invalid_encoding, tmp_path / 'missing.yaml'):
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            codec().read_yaml(path)


def test_setup_yaml_checks_nesting_before_constructing_deep_content(tmp_path):
    accepted = 'value: ' + '[' * 31 + 'null' + ']' * 31 + '\n'
    assert isinstance(codec().read_yaml(document(tmp_path, accepted)), dict)
    for depth in (32, 2000):
        text = 'value: ' + '[' * depth + 'null' + ']' * depth + '\n'
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            codec().read_yaml(document(tmp_path, text))


def test_setup_yaml_writer_does_not_emit_aliases_for_shared_values(tmp_path):
    shared = ['read-only']
    value = {'first': shared, 'second': shared}
    encoded = codec().write_yaml(value)
    assert '&id' not in encoded and '*id' not in encoded
    assert codec().read_yaml(document(tmp_path, encoded)) == value


def test_setup_yaml_writer_quotes_strings_that_look_like_numbers(tmp_path):
    value = {'values': ['1e3', '1E+2', '.inf', '.nan', 'true', 'null', '123', '001']}
    assert codec().read_yaml(document(tmp_path, codec().write_yaml(value))) == value


@pytest.mark.parametrize('value', [
    {'number': math.nan}, {'number': math.inf}, {1: 'number key'},
    {'value': {'not-json'}}, {'value': (1, 2)}, [], None,
])
def test_setup_yaml_writer_rejects_values_outside_the_contract(value):
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        codec().write_yaml(value)


def test_setup_yaml_writer_rejects_cycles():
    value = {}
    value['self'] = value
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        codec().write_yaml(value)
