"""Structured setup validates answers without granting or replacing other settings."""

import copy
import json
import subprocess
import sys

import pytest

from daeri import setup
from daeri.storage import Store, StoreError


def values_file(tmp_path, values):
    path = tmp_path / 'answers.json'
    path.write_text(json.dumps(values), encoding='utf-8')
    return path


def source_values():
    return {'server': 'https://git.example.invalid'}


def cache_resource(store, instance='cache-one'):
    resource = setup.profile_template(store, 'cache-redis', 'service', instance)['resources'][0]
    resource['target']['host'] = 'cache.example.invalid'
    resource['connection']['env']['REDIS_HOST'] = 'cache.example.invalid'
    resource['capabilities'][0]['constraints'] = {'allowed_values': {'key': ['service:status']}}
    return resource


def cli(tmp_path, *args):
    return subprocess.run([sys.executable, '-m', 'daeri.cli', '--data-dir',
                           str(tmp_path / 'absent'), 'resource', *args],
                          text=True, capture_output=True, timeout=20)


def test_schema_and_partial_validation_do_not_create_user_data_or_choose_defaults(tmp_path):
    schema = cli(tmp_path, 'setup-schema', 'db-postgres')
    assert schema.returncode == 0, schema.stdout + schema.stderr
    contract = json.loads(schema.stdout)
    assert contract['input_schema']['properties']['port']['default'] == 5432
    assert 'port' in contract['input_schema']['required']
    checked = cli(tmp_path, 'validate-setup', 'db-postgres',
                  str(values_file(tmp_path, {'host': 'db.example.invalid'})))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    result = json.loads(checked.stdout)
    assert result['valid'] is True and result['complete'] is False
    assert {'port', 'database', 'username'} <= set(result['missing_fields'])
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('values,field', [
    ({'port': True}, 'port'), ({'port': '5432'}, 'port'), ({'port': 5432.0}, 'port'),
    ({'port': 0}, 'port'), ({'port': 65536}, 'port'), ({'host': ''}, 'host'),
])
def test_partial_validation_identifies_invalid_fields_without_returning_values(tmp_path, values, field):
    result = cli(tmp_path, 'validate-setup', 'db-postgres', str(values_file(tmp_path, values)))
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report['valid'] is False and report['complete'] is False
    assert any(issue['field'] == field for issue in report['issues'])
    assert all(set(issue) == {'field', 'code'} for issue in report['issues'])
    assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('values', [
    {'password': 'SETUP_PRIVATE_SENTINEL'},
    {'host': 'password=SETUP_PRIVATE_SENTINEL'},
    {'SETUP_PRIVATE_SENTINEL': 'unknown-field'},
])
def test_invalid_setup_answers_never_echo_secrets_or_unknown_field_names(tmp_path, values):
    result = cli(tmp_path, 'validate-setup', 'db-postgres', str(values_file(tmp_path, values)))
    assert result.returncode != 0
    assert 'SETUP_PRIVATE_SENTINEL' not in result.stdout + result.stderr
    assert not (tmp_path / 'absent').exists()


def test_configure_adds_one_resource_and_preserves_profile_settings(tmp_path):
    with_store = Store(tmp_path / 'private')
    try:
        before = with_store.put_profile('service', core_context=['기존 운영 맥락'], retention_days=17,
                                        long_term_memory_days=80, memory_policy={'model_allowed': False})
        after = setup.configure_resource(with_store, 'source-gitlab', 'service', 'source-one',
                                          values_file(tmp_path, source_values()), expected_revision=1)
        assert after['revision'] == 2
        assert after['core_context'] == before['core_context']
        assert after['retention_days'] == 17 and after['long_term_memory_days'] == 80
        assert after['memory_policy'] == before['memory_policy']
        resource = after['resources'][0]
        assert resource['target'] == {'server': 'https://git.example.invalid'}
        assert resource['connection']['env']['GITLAB_URL'] == 'https://git.example.invalid'
        assert resource['capabilities'][0]['constraints'] == {}
        assert resource['policy'] is None
        assert resource['connection']['auth_env'] == {
            'GITLAB_TOKEN': 'credential:service.source-one.gitlab_token'}
        assert not (with_store.data_dir / 'tools').exists()
    finally:
        with_store.close()


def test_repeated_setup_updates_only_supplied_fields_and_preserves_other_resources(tmp_path):
    store = Store(tmp_path / 'private')
    try:
        source = setup.profile_template(store, 'source-gitlab', 'service', 'source-one')['resources'][0]
        source['adapter'] = 'source_read'
        source['target'] = {'server': 'https://git.example.invalid'}
        source['connection']['env']['GITLAB_URL'] = 'https://git.example.invalid'
        source['capabilities'][0]['constraints'] = {
            'fixed': {'project_id': 'team/service'}, 'allowed_values': {'ref': ['release']},
            'path_prefixes': {'file_path': ['lib']}}
        source['policy'] = {'masking': []}
        cache = cache_resource(store)
        before = store.put_profile('service', resources=[source, cache], retention_days=12)
        after = setup.configure_resource(store, 'source-gitlab', 'service', 'source-one',
                                          values_file(tmp_path, {'server': 'https://new-git.example.invalid'}), expected_revision=1)
        expected = copy.deepcopy(before['resources'])
        expected[0]['target']['server'] = 'https://new-git.example.invalid'
        expected[0]['connection']['env']['GITLAB_URL'] = 'https://new-git.example.invalid'
        assert after['resources'] == expected
        assert after['retention_days'] == 12
    finally:
        store.close()


def test_resource_builder_creates_complete_resource_without_publishing_a_profile(tmp_path):
    store = Store(tmp_path / 'private')
    try:
        values = source_values()
        supplied = copy.deepcopy(values)
        resource = setup.build_setup_resource(store, 'source-gitlab', 'service', 'source-one', values)
        assert resource['target'] == {'server': 'https://git.example.invalid'}
        assert resource['capabilities'][0]['constraints'] == {}
        assert resource['policy'] is None
        assert values == supplied
        resource['capabilities'][0]['constraints']['fixed'] = {'project_id': 'team/another'}
        assert values == supplied
        assert store.list_profiles() == []
        assert not (store.data_dir / 'tools').exists()
    finally:
        store.close()


@pytest.mark.parametrize('account,identifier', [
    (123456789012, '123456789012'), (100000000000, '100000000000'),
    (999999999999, '999999999999'), ('012345678901', '012345678901'),
])
def test_infra_setup_stores_valid_numeric_accounts_as_identifiers(tmp_path, account, identifier):
    supplied = {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': account}
    path = values_file(tmp_path, supplied)
    assert setup.validate_setup('infra-aws', path)['complete'] is True
    store = Store(tmp_path / 'private')
    try:
        store.put_profile('service')
        saved = setup.configure_resource(store, 'infra-aws', 'service', 'infra-one', path,
                                         expected_revision=1)
        assert saved['resources'][0]['target']['account'] == identifier
        assert type(saved['resources'][0]['target']['account']) is str
        assert json.loads(path.read_text(encoding='utf-8')) == supplied
        canonical = values_file(tmp_path, {'account': identifier})
        assert setup.configure_resource(store, 'infra-aws', 'service', 'infra-one', canonical,
                                        expected_revision=2) == saved
    finally:
        store.close()


@pytest.mark.parametrize('account', [True, False, 123456789012.0, -123456789012,
                                     12345678901, 1234567890123, '123', '123456789012.0'])
def test_infra_account_validation_rejects_values_without_a_twelve_digit_identifier(tmp_path, account):
    values = {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': account}
    report = setup.validate_setup('infra-aws', values_file(tmp_path, values))
    assert report['valid'] is False
    assert report['complete'] is False
    assert any(issue['field'] == 'account' for issue in report['issues'])
    store = Store(tmp_path / 'private')
    try:
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            setup.build_setup_resource(store, 'infra-aws', 'service', 'infra-one', values)
        assert store.list_profiles() == []
    finally:
        store.close()


def test_resource_builder_merges_partial_values_without_mutating_existing_resource(tmp_path):
    store = Store(tmp_path / 'private')
    try:
        existing = setup.build_setup_resource(store, 'source-gitlab', 'service', 'source-one', source_values())
        existing['policy'] = {'masking': []}
        existing['capabilities'][0]['constraints'] = {
            'fixed': {'project_id': 'team/service'}, 'allowed_values': {'ref': ['main']},
            'path_prefixes': {'file_path': ['src']}}
        before = copy.deepcopy(existing)
        values = {'server': 'https://new-git.example.invalid'}
        result = setup.build_setup_resource(store, 'source-gitlab', 'service', 'source-one', values, existing)
        assert result['target'] == {'server': 'https://new-git.example.invalid'}
        assert result['capabilities'][0]['constraints'] == {
            'fixed': {'project_id': 'team/service'}, 'allowed_values': {'ref': ['main']},
            'path_prefixes': {'file_path': ['src']}}
        assert result['policy'] == {'masking': []}
        result['policy']['masking'].append({'columns': ['email']})
        result['capabilities'][0]['constraints']['allowed_values']['ref'].append('main')
        assert existing == before
        assert values == {'server': 'https://new-git.example.invalid'}
        assert store.list_profiles() == []
    finally:
        store.close()


def test_resource_builder_rejects_missing_new_resource_values_before_any_publication(tmp_path):
    store = Store(tmp_path / 'private')
    try:
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            setup.build_setup_resource(store, 'source-gitlab', 'service', 'source-one',
                                       {})
        assert store.list_profiles() == []
    finally:
        store.close()


@pytest.mark.parametrize('values,revision,code', [
    ({}, 1, 'INVALID_INPUT'),
    ({'unexpected': 'value'}, 1, 'INVALID_INPUT'),
    (source_values(), 0, 'REVISION_CONFLICT'),
])
def test_failed_setup_keeps_active_profile_and_published_files(tmp_path, values, revision, code):
    store = Store(tmp_path / 'private')
    try:
        before = store.put_profile('service', core_context=['保留'], retention_days=7)
        files = sorted((store.data_dir / 'profiles').iterdir())
        with pytest.raises(StoreError) as caught:
            setup.configure_resource(store, 'source-gitlab', 'service', 'source-one',
                                     values_file(tmp_path, values), expected_revision=revision)
        assert caught.value.code == code
        assert store.get_profile('service') == before
        assert sorted((store.data_dir / 'profiles').iterdir()) == files
    finally:
        store.close()


def test_configure_refuses_replacing_another_adapter_at_the_same_instance(tmp_path):
    store = Store(tmp_path / 'private')
    try:
        cache = cache_resource(store, 'shared')
        before = store.put_profile('service', resources=[cache])
        with pytest.raises(StoreError):
            setup.configure_resource(store, 'source-gitlab', 'service', 'shared',
                                     values_file(tmp_path, source_values()), expected_revision=1)
        assert store.get_profile('service') == before
    finally:
        store.close()


def test_new_cache_setup_does_not_confirm_tls_without_an_answer(tmp_path):
    report = setup.validate_setup('cache-redis', values_file(tmp_path, {
        'host': 'cache.example.invalid', 'port': 6379, 'db': 0}))
    assert report['valid'] is True
    assert report['complete'] is False
    assert 'tls' in report['missing_fields']


def test_adapter_object_field_cannot_bypass_its_nested_required_values(tmp_path, monkeypatch):
    from daeri.adapters import get_adapter
    adapter = get_adapter('postgres_read')
    fields = copy.deepcopy(adapter.setup_fields)
    fields['options'] = {'required': False, 'paths': [['target', 'options']],
                        'schema': {'type': 'object', 'properties': {'host': {'type': 'string'}},
                                   'required': ['host'], 'additionalProperties': False}}
    monkeypatch.setattr(adapter, 'setup_fields', fields)
    report = setup.validate_setup('db-postgres', values_file(tmp_path, {'options': {}}))
    assert report['valid'] is False
    assert {'field': 'options', 'code': 'INVALID_VALUE'} in report['issues']


@pytest.mark.parametrize('recipe,values,target,environment,constraints', [
    ('db-postgres', {'host': 'db.example.invalid', 'port': 5544, 'database': 'service',
                     'username': 'reader'},
     {'host': 'db.example.invalid', 'port': 5544, 'database': 'service', 'username': 'reader'},
     {}, {}),
    ('cache-redis', {'host': 'cache.example.invalid', 'port': 6380, 'db': 1, 'tls': False,
                     'username': 'reader'},
     {'host': 'cache.example.invalid', 'port': 6380, 'db': 1},
     {'REDIS_HOST': 'cache.example.invalid', 'REDIS_PORT': '6380', 'REDIS_DB': '1',
      'REDIS_SSL': 'false', 'REDIS_USERNAME': 'reader'}, {}),
    ('infra-aws', {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': '111111111111'},
     {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': '111111111111'},
     {'AWS_PROFILE': 'operations-reader', 'AWS_REGION': 'ap-northeast-2'},
     {'command_field': 'cli_command', 'region': 'ap-northeast-2'}),
    ('source-gitlab', {'server': 'https://git.example.invalid'},
     {'server': 'https://git.example.invalid'}, {'GITLAB_URL': 'https://git.example.invalid'}, {}),
    ('document-confluence', {'server': 'https://docs.example.invalid/wiki',
                             'username': 'reader@example.invalid'},
     {'server': 'https://docs.example.invalid/wiki'},
     {'CONFLUENCE_URL': 'https://docs.example.invalid/wiki',
      'CONFLUENCE_USERNAME': 'reader@example.invalid'}, {}),
])
def test_explicit_setup_values_keep_target_connection_and_scope_in_sync(
        tmp_path, recipe, values, target, environment, constraints):
    path = values_file(tmp_path, values)
    assert setup.validate_setup(recipe, path)['complete'] is True
    store = Store(tmp_path / 'private')
    try:
        store.put_profile('service')
        saved = setup.configure_resource(store, recipe, 'service', 'resource-one', path, expected_revision=1)
        resource = saved['resources'][0]
        assert resource['target'] == target
        assert environment.items() <= resource['connection']['env'].items()
        if recipe == 'document-confluence':
            assert 'CONFLUENCE_SPACES_FILTER' not in resource['connection']['env']
        assert resource['capabilities'][0]['constraints'] == constraints
        assert setup.configure_resource(store, recipe, 'service', 'resource-one', path,
                                        expected_revision=2) == saved
    finally:
        store.close()


def test_configure_cli_requires_the_inspected_revision_and_saves_only_valid_answers(tmp_path):
    created = subprocess.run([sys.executable, '-m', 'daeri.cli', '--data-dir', str(tmp_path / 'absent'),
                              'profile', 'create', 'service'], text=True, capture_output=True, timeout=20)
    assert created.returncode == 0, created.stdout + created.stderr
    path = values_file(tmp_path, source_values())
    args = ('configure', 'source-gitlab', '--profile', 'service', '--instance', 'source-one',
            '--values', str(path))
    assert cli(tmp_path, *args).returncode == 2
    saved = cli(tmp_path, *args, '--expected-revision', '1')
    assert saved.returncode == 0, saved.stdout + saved.stderr
    assert json.loads(saved.stdout)['resources'][0]['target'] == {'server': 'https://git.example.invalid'}
    stale = cli(tmp_path, *args, '--expected-revision', '1')
    assert stale.returncode == 1
    assert json.loads(stale.stdout)['error']['code'] == 'REVISION_CONFLICT'


@pytest.mark.parametrize('recipe,values,constraints', [
    ('db-postgres', {'host': 'db.example.invalid', 'port': 5432, 'database': 'service',
                     'username': 'reader'},
     {'sql_field': 'sql', 'tables': ['public.health'], 'columns': ['id'], 'max_rows': 25}),
    ('cache-redis', {'host': 'cache.example.invalid', 'port': 6379, 'db': 0, 'tls': True},
     {'allowed_values': {'key': ['service:status']}}),
    ('source-gitlab', {'server': 'https://git.example.invalid'},
     {'fixed': {'project_id': 'team/service'}, 'allowed_values': {'ref': ['main']},
      'path_prefixes': {'file_path': ['src']}}),
    ('document-confluence', {'server': 'https://docs.example.invalid', 'username': 'reader'},
     {'allowed_values': {'page_id': ['123456']}}),
])
def test_connection_setup_preserves_imported_resource_restrictions(tmp_path, recipe, values, constraints):
    store = Store(tmp_path / 'private')
    try:
        resource = setup.profile_template(store, recipe, 'service', 'resource-one')['resources'][0]
        resource['capabilities'][0]['constraints'] = constraints
        if recipe == 'source-gitlab':
            resource['target']['project'] = 'team/service'
        if recipe == 'document-confluence':
            resource['target']['space'] = 'OPS'
            resource['connection']['env']['CONFLUENCE_SPACES_FILTER'] = 'OPS'
        before = store.put_profile('service', resources=[resource])
        configured = setup.configure_resource(store, recipe, 'service', 'resource-one',
                                              values_file(tmp_path, values), expected_revision=1)
        saved = configured['resources'][0]
        assert saved['capabilities'] == before['resources'][0]['capabilities']
        for key in ('project', 'space'):
            if key in resource['target']:
                assert saved['target'][key] == resource['target'][key]
        if recipe == 'document-confluence':
            assert saved['connection']['env']['CONFLUENCE_SPACES_FILTER'] == 'OPS'
        exported = setup.export_profile(store, 'service')
        restored = setup.import_profile(store, values_file(tmp_path, exported))
        assert restored['resources'] == configured['resources']
    finally:
        store.close()


@pytest.mark.parametrize('recipe,values', [
    ('db-postgres', {'tables': ['public.orders'], 'columns': ['id'], 'max_rows': 10}),
    ('cache-redis', {'keys': ['service:status']}),
    ('source-gitlab', {'project': 'team/service', 'refs': ['main'], 'path_prefixes': ['src']}),
    ('document-confluence', {'space': 'OPS', 'page_ids': ['123456']}),
])
def test_setup_keeps_work_scope_out_of_connection_input(tmp_path, recipe, values):
    report = setup.validate_setup(recipe, values_file(tmp_path, values))
    assert report['valid'] is False
    assert {'field': '', 'code': 'UNKNOWN_FIELD'} in report['issues']
