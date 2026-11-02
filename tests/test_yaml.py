import pytest

from daeri.core.errors import StoreError


def codec():
    import daeri.config.yaml as config_yaml
    return config_yaml


def document(tmp_path, text):
    path = tmp_path / 'cloud.yaml'
    path.write_text(text, encoding='utf-8')
    return path


def test_yaml_uses_json_compatible_scalar_types(tmp_path):
    value = codec().read_yaml(document(tmp_path, '''name: service
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


def test_yaml_accepts_comments_and_caller_owned_secret_validation(tmp_path):
    value = codec().read_yaml(document(tmp_path, '# public comment\nname: service\npassword: caller-validates\n'))
    assert value == {'name': 'service', 'password': 'caller-validates'}


def test_yaml_parses_exact_utf8_bytes():
    assert codec().parse_yaml('name: 운영\n'.encode('utf-8')) == {'name': '운영'}


@pytest.mark.parametrize('content', [b'name: \xff', b'key: 1\nkey: 2\n',
                                     b'#' * (2 * 1024 * 1024 + 1), 'name: service'])
def test_yaml_bytes_parser_uses_the_same_bounds_and_validation(content):
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        codec().parse_yaml(content)


@pytest.mark.parametrize('text', [
    'name: one\nname: secret-input\n',
    'resources: {same: 1, same: secret-input}\n',
    'name: &secret-input service\n',
    'name: *secret-input\n',
    'name: !!str secret-input\n',
    'name: !custom secret-input\n',
    'resources: {<<: {secret-input: value}}\n',
    'resources: {"<<": {secret-input: value}}\n',
    'name: one\n---\nname: secret-input\n',
    'name: .nan\n', 'name: .inf\n', 'name: -.Inf\n',
    'name: 1e999\n',
    '1: secret-input\n', 'false: secret-input\n', 'null: secret-input\n',
    '? [secret-input]\n: value\n',
    '[secret-input]\n', 'secret-input\n', '',
    'name: [secret-input\n',
    '%TAG !foo! tag:example.test,2026:\n---\nname: secret-input\n',
])
def test_yaml_rejects_unsafe_or_ambiguous_documents_without_echoing_values(tmp_path, text):
    with pytest.raises(StoreError, match='INVALID_INPUT') as failure:
        codec().read_yaml(document(tmp_path, text))
    assert 'secret-input' not in str(failure.value)


def test_yaml_syntax_error_has_safe_location(tmp_path):
    with pytest.raises(StoreError) as failure:
        codec().read_yaml(document(tmp_path, 'name: [private-input\n'))
    assert '행' in str(failure.value)
    assert '열' in str(failure.value)
    assert 'private-input' not in str(failure.value)


def test_yaml_rejects_oversized_and_non_utf8_files(tmp_path):
    oversized = tmp_path / 'oversized.yaml'
    oversized.write_bytes(b'#' * (2 * 1024 * 1024 + 1))
    invalid_encoding = tmp_path / 'encoding.yaml'
    invalid_encoding.write_bytes(b'name: \xff\n')
    for path in (oversized, invalid_encoding, tmp_path / 'missing.yaml'):
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            codec().read_yaml(path)


def test_yaml_checks_nesting_before_constructing_deep_content(tmp_path):
    accepted = 'value: ' + '[' * 31 + 'null' + ']' * 31 + '\n'
    assert isinstance(codec().read_yaml(document(tmp_path, accepted)), dict)
    for depth in (32, 2000):
        text = 'value: ' + '[' * depth + 'null' + ']' * depth + '\n'
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            codec().read_yaml(document(tmp_path, text))
