"""Adapter-owned setup fields reject unsafe or incomplete local configuration."""

import pytest
from jsonschema import Draft202012Validator

from daeri.adapters import BUILTIN_ADAPTERS, get_adapter


def field(adapter, name):
    return get_adapter(adapter).setup_fields[name]


def accepts(adapter, name, value):
    return Draft202012Validator(field(adapter, name)['schema']).is_valid(value)


@pytest.mark.parametrize('adapter', BUILTIN_ADAPTERS.values(), ids=lambda item: item.adapter_id)
def test_setup_describes_every_target_field_without_collecting_secrets(adapter):
    fields = adapter.setup_fields
    assert fields
    target_fields = {path[1] for item in fields.values() for path in item['paths'] if path[0] == 'target'}
    assert set(adapter.target_template) <= target_fields
    for name, item in fields.items():
        assert set(item) == {'schema', 'required', 'paths'}
        assert type(item['required']) is bool
        assert item['paths']
        assert all(path[0] in {'target', 'connection', 'capabilities'} for path in item['paths'])
        assert all('auth_env' not in path and 'auth_headers' not in path for path in item['paths'])
        schema = item['schema']
        Draft202012Validator.check_schema(schema)
        assert schema['title'] and schema['description']
        assert schema.get('examples')
        for example in schema['examples']:
            assert accepts(adapter.adapter_id, name, example)
        if 'default' in schema:
            assert accepts(adapter.adapter_id, name, schema['default'])


@pytest.mark.parametrize('adapter,name,value', [
    ('postgres_read', 'port', 0), ('postgres_read', 'port', 65536),
    ('postgres_read', 'port', True), ('postgres_read', 'port', '5432'),
    ('postgres_read', 'host', 'db\x00.example.test'),
    ('redis_read', 'db', -1), ('redis_read', 'tls', 'true'),
    ('aws_read', 'account', '123'), ('aws_read', 'account', 12345678901),
    ('aws_read', 'account', 1234567890123), ('aws_read', 'account', -123456789012),
    ('aws_read', 'account', True),
    ('gitlab_read', 'server', 'http://git.example.test'),
    ('gitlab_read', 'server', 'https://user:password@git.example.test'),
    ('gitlab_read', 'server', 'https://git.example.test/\x00private'),
    ('confluence_read', 'server', 'https://docs.example.test\\private'),
])
def test_setup_rejects_values_that_cannot_represent_the_selected_binding(adapter, name, value):
    assert not accepts(adapter, name, value)


@pytest.mark.parametrize('account', ['012345678901', '123456789012', 100000000000, 999999999999])
def test_infra_setup_accepts_twelve_digit_account_strings_and_integers(account):
    assert accepts('aws_read', 'account', account)


@pytest.mark.parametrize('adapter,names', [
    ('postgres_read', {'host', 'port', 'database', 'username'}),
    ('redis_read', {'host', 'port', 'db', 'tls', 'username'}),
    ('aws_read', {'profile', 'region', 'account'}),
    ('gitlab_read', {'server'}),
    ('confluence_read', {'server', 'username'}),
    ('jira_read', {'server', 'auth_type', 'username'}),
])
def test_setup_collects_connection_fields_for_every_resource_category(adapter, names):
    fields = get_adapter(adapter).setup_fields
    assert set(fields) == names


def test_setup_does_not_limit_provider_profile_names_to_an_identifier_alphabet():
    assert accepts('aws_read', 'profile', 'operations reader@example.test')


@pytest.mark.parametrize('name', ['operations[reader', 'operations]reader', 'operations\nreader',
                                 'operations\rreader', 'operations\x00reader', 'operations\n', '', 'a' * 129])
def test_setup_rejects_provider_profile_names_that_cannot_be_safely_written_to_ini(name):
    assert not accepts('aws_read', 'profile', name)
