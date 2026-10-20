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
    return {'server': 'https://git.example.invalid', 'project': 'team/service',
            'refs': ['main'], 'path_prefixes': ['src']}


def cache_resource(store, instance='cache-one'):
    resource = setup.profile_template(store, 'cache-redis', 'service', instance)['resources'][0]
    resource['target']['host'] = 'cache.example.invalid'
    resource['connection']['env']['REDIS_HOST'] = 'cache.example.invalid'
    resource['capabilities'][0]['constraints']['allowed_values']['key'] = ['service:status']
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
    ({'tables': []}, 'tables'), ({'tables': ['public.users', False]}, 'tables'),
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
        assert resource['target'] == {'server': 'https://git.example.invalid', 'project': 'team/service'}
        assert resource['connection']['env']['GITLAB_URL'] == 'https://git.example.invalid'
        assert resource['capabilities'][0]['constraints'] == {
            'fixed': {'project_id': 'team/service'}, 'allowed_values': {'ref': ['main']},
            'path_prefixes': {'file_path': ['src']}}
        assert resource['policy'] == {}
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
        source['target'] = {'server': 'https://git.example.invalid', 'project': 'team/service'}
        source['connection']['env']['GITLAB_URL'] = 'https://git.example.invalid'
        source['capabilities'][0]['constraints'] = {
            'fixed': {'project_id': 'team/service'}, 'allowed_values': {'ref': ['release']},
            'path_prefixes': {'file_path': ['lib']}}
        source['policy'] = {'masking': []}
        cache = cache_resource(store)
        before = store.put_profile('service', resources=[source, cache], retention_days=12)
        after = setup.configure_resource(store, 'source-gitlab', 'service', 'source-one',
                                          values_file(tmp_path, {'refs': ['main']}), expected_revision=1)
        expected = copy.deepcopy(before['resources'])
        expected[0]['capabilities'][0]['constraints']['allowed_values']['ref'] = ['main']
        assert after['resources'] == expected
        assert after['retention_days'] == 12
    finally:
        store.close()


@pytest.mark.parametrize('values,revision,code', [
    ({'server': 'https://git.example.invalid'}, 1, 'INVALID_INPUT'),
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
        'host': 'cache.example.invalid', 'port': 6379, 'db': 0, 'keys': ['service:status']}))
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
                     'username': 'reader', 'tables': ['public.health'], 'columns': [], 'max_rows': 25},
     {'host': 'db.example.invalid', 'port': 5544, 'database': 'service', 'username': 'reader'},
     {}, {'sql_field': 'sql', 'tables': ['public.health'], 'columns': [], 'max_rows': 25}),
    ('db-postgres', {'host': 'db.example.invalid', 'port': 5432, 'database': 'service',
                     'username': 'reader', 'tables': ['*'], 'columns': ['*'], 'max_rows': 25},
     {'host': 'db.example.invalid', 'port': 5432, 'database': 'service', 'username': 'reader'},
     {}, {'sql_field': 'sql', 'tables': ['*'], 'columns': ['*'], 'max_rows': 25}),
    ('cache-redis', {'host': 'cache.example.invalid', 'port': 6380, 'db': 1, 'tls': False,
                     'username': 'reader', 'keys': ['service:status']},
     {'host': 'cache.example.invalid', 'port': 6380, 'db': 1},
     {'REDIS_HOST': 'cache.example.invalid', 'REDIS_PORT': '6380', 'REDIS_DB': '1',
      'REDIS_SSL': 'false', 'REDIS_USERNAME': 'reader'}, {'allowed_values': {'key': ['service:status']}}),
    ('infra-aws', {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': '111111111111'},
     {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': '111111111111'},
     {'AWS_PROFILE': 'operations-reader', 'AWS_REGION': 'ap-northeast-2'},
     {'command_field': 'cli_command', 'region': 'ap-northeast-2'}),
    ('document-confluence', {'server': 'https://docs.example.invalid/wiki', 'space': 'OPS',
                             'username': 'reader@example.invalid', 'page_ids': ['123456']},
     {'server': 'https://docs.example.invalid/wiki', 'space': 'OPS'},
     {'CONFLUENCE_URL': 'https://docs.example.invalid/wiki', 'CONFLUENCE_SPACES_FILTER': 'OPS',
      'CONFLUENCE_USERNAME': 'reader@example.invalid'}, {'allowed_values': {'page_id': ['123456']}}),
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
    assert json.loads(saved.stdout)['resources'][0]['target']['project'] == 'team/service'
    stale = cli(tmp_path, *args, '--expected-revision', '1')
    assert stale.returncode == 1
    assert json.loads(stale.stdout)['error']['code'] == 'REVISION_CONFLICT'


def test_db_setup_preserves_restrictions_until_whole_access_is_explicit_and_portable(tmp_path):
    store = Store(tmp_path / 'private')
    try:
        store.put_profile('service')
        answers = {'host': 'db.example.invalid', 'port': 5432, 'database': 'service',
                   'username': 'reader', 'tables': ['public.health'], 'columns': ['id'], 'max_rows': 25}
        setup.configure_resource(store, 'db-postgres', 'service', 'db-one',
                                 values_file(tmp_path, answers), expected_revision=1)
        unchanged = setup.configure_resource(store, 'db-postgres', 'service', 'db-one',
                                            values_file(tmp_path, {'max_rows': 30}), expected_revision=2)
        assert unchanged['resources'][0]['capabilities'][0]['constraints'] == {
            'sql_field': 'sql', 'tables': ['public.health'], 'columns': ['id'], 'max_rows': 30}
        whole = setup.configure_resource(store, 'db-postgres', 'service', 'db-one',
                                        values_file(tmp_path, {'tables': ['*'], 'columns': ['*']}), expected_revision=3)
        exported = setup.export_profile(store, 'service')
        assert exported['resources'][0]['capabilities'][0]['constraints'] == {
            'sql_field': 'sql', 'tables': ['*'], 'columns': ['*'], 'max_rows': 30}
        restored = setup.import_profile(store, values_file(tmp_path, exported))
        assert restored['resources'] == whole['resources']
    finally:
        store.close()
