import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from daeri.config import setup
from daeri.config.setup import export_profile, import_profile, load_catalog, profile_template, prepare_resource, prepared_executable
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'private')
    yield value
    value.close()


@pytest.fixture
def private_uv(tmp_path, monkeypatch):
    plugin = tmp_path / 'installed-plugin'
    executable = plugin / '.runtime' / 'uv.exe'
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(sys, 'prefix', str(plugin / '.venv'))
    return executable


def resource():
    return {'instance_id': 'source-one', 'type': 'Source', 'adapter': 'source_read',
            'target': {'server': 'https://git.example.test', 'project': 'team/service'},
            'identity_ref': 'identity:reader',
            'connection': {'transport': 'stdio', 'command': sys.executable,
                           'args': ['fake_mcp.py'], 'env': {'GITLAB_URL': 'https://git.example.test'},
                           'auth_env': {'GITLAB_TOKEN': 'credential:service.source'}},
            'capabilities': [{'capability_id': 'read-file', 'tool_name': 'get_repository_file',
                              'constraints': {'fixed': {'project_id': 'team/service'},
                                              'allowed_values': {'ref': ['main']},
                                              'path_prefixes': {'file_path': ['src']}}}],
            'policy': None}


def config_file(tmp_path, value):
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(value))
    return path


def test_import_and_export_keep_only_portable_configuration(store, tmp_path):
    config = {'name': 'service', 'core_context': ['UTC 운영'], 'resources': [resource()],
              'retention_days': 15, 'long_term_memory_days': 90, 'memory_policy': None}
    result = import_profile(store, config_file(tmp_path, config))
    assert result['revision'] == 1
    assert export_profile(store, 'service') == {**config, 'embedding': {'provider': 'local'}}
    assert import_profile(store, config_file(tmp_path, config))['revision'] == 2


@pytest.mark.parametrize('url', [['https://mcp.example.test/mcp'], {'url': 'https://mcp.example.test/mcp'}, 1,
                                 'https://[invalid'])
def test_import_cli_returns_invalid_input_for_malformed_remote_urls(tmp_path, monkeypatch, capsys, url):
    from daeri.cli import main

    item = resource()
    item['connection'] = {'transport': 'streamable_http', 'url': url}
    path = config_file(tmp_path, {'name': 'service', 'resources': [item]})
    monkeypatch.setattr(sys, 'argv', ['daeri', '--data-dir', str(tmp_path / 'private'), 'profile', 'import', str(path)])
    assert main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)['error']['code'] == 'INVALID_INPUT'
    assert captured.err == ''
    with closing(Store(tmp_path / 'private')) as store:
        assert store.list_profiles() == []


@pytest.mark.parametrize('origin', ['posix', 'windows'])
def test_import_relocates_managed_recipe_commands_without_changing_configuration(store, tmp_path, origin):
    from pathlib import PureWindowsPath
    from daeri.config.setup import build_setup_resource
    answers = {
        'db-postgres': {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'},
        'cache-redis': {'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True},
        'infra-aws': {'profile': 'operations-reader', 'region': 'ap-northeast-2', 'account': '111111111111'},
        'source-gitlab': {'server': 'https://git.example.test'},
        'document-confluence': {'server': 'https://docs.example.test/wiki', 'username': 'reader@example.test'},
        'issue-jira': {'server': 'https://issues.example.test', 'auth_type': 'personal_token'},
    }
    resources = [build_setup_resource(store, recipe, 'service', recipe, values)
                 for recipe, values in answers.items()]
    source = next(value for value in resources if value['instance_id'] == 'source-gitlab')
    source['adapter'] = 'source_read'
    source['identity_ref'] = 'identity:shared-source'
    source['target']['project'] = 'team/service'
    source['connection']['auth_env']['GITLAB_TOKEN'] = 'credential:shared.source'
    source['capabilities'][0]['constraints'] = {'fixed': {'project_id': 'team/service'}}
    source['policy'] = {'allowed_model_fields': [{'segments': [{'key': 'body'}]}], 'export_fields': []}
    manual = resource()
    manual['instance_id'] = 'manual'
    store.put_profile('service', resources=[*resources, manual], core_context=['UTC 운영'],
                      retention_days=15, long_term_memory_days=90,
                      memory_policy={'categories': ['lesson'], 'instance_ids': ['source-gitlab'],
                                     'model_allowed': True, 'retain_allowed': False},
                      embedding={'provider': 'keyword_only'})
    exported = export_profile(store, 'service')
    expected = copy.deepcopy(exported)
    with closing(Store(tmp_path / 'new-pc')) as destination:
        for supplied, relocated in zip(exported['resources'][:-1], expected['resources'][:-1]):
            relative = Path(supplied['connection']['command']).relative_to(store.data_dir)
            relocated['connection']['command'] = str(destination.data_dir / relative)
            if origin == 'windows':
                parts = list(relative.parts)
                parts[-2:] = ['Scripts', parts[-1] + '.exe']
                supplied['connection']['command'] = str(PureWindowsPath('C:/Users/old/DAERI', *parts))
        imported = import_profile(destination, config_file(tmp_path, exported))
        assert export_profile(destination, 'service') == expected
        assert imported['memory_policy'] == store.get_profile('service')['memory_policy']
        assert not (destination.data_dir / 'tools').exists()


@pytest.mark.parametrize('change', ['outside-tools', 'unknown-pin'])
def test_import_preserves_explicit_manual_recipe_executables(store, tmp_path, change):
    configured = profile_template(store, 'source-gitlab', 'service', 'source-one')['resources'][0]
    command = Path(configured['connection']['command'])
    if change == 'outside-tools':
        command = tmp_path / 'custom' / command.name
    else:
        command = command.parents[2].with_name('0.0.0-' + '0' * 16) / '.venv' / command.parent.name / command.name
    configured['connection']['command'] = str(command)
    imported = import_profile(store, config_file(tmp_path, {'name': 'service', 'resources': [configured]}))
    assert imported['resources'] == [configured]


def test_unlimited_memory_retention_round_trips_without_replacing_other_settings(store, tmp_path):
    config = {'name': 'service', 'core_context': ['UTC 운영'], 'resources': [resource()],
              'retention_days': 15, 'long_term_memory_days': None, 'memory_policy': None}
    profile = import_profile(store, config_file(tmp_path, config))
    assert profile['long_term_memory_days'] is None
    assert export_profile(store, 'service') == {**config, 'embedding': {'provider': 'local'}}
    assert import_profile(store, config_file(tmp_path, export_profile(store, 'service')))['retention_days'] == 15


def test_all_retention_limits_can_be_unlimited_in_profile_import_export(store, tmp_path):
    config = {'name': 'service', 'core_context': ['UTC 운영'], 'resources': [resource()],
              'retention_days': None, 'long_term_memory_days': None, 'memory_policy': None}
    profile = import_profile(store, config_file(tmp_path, config))
    assert profile['retention_days'] is None
    assert export_profile(store, 'service') == {**config, 'embedding': {'provider': 'local'}}


@pytest.mark.parametrize('field', ['core_context', 'resources', 'embedding'])
def test_profile_import_rejects_null_collections_without_replacing_settings(store, tmp_path, field):
    original = store.put_profile('service', core_context=['UTC 운영'], resources=[resource()],
                                 embedding={'provider': 'keyword_only'})
    supplied = export_profile(store, 'service')
    supplied[field] = None
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        import_profile(store, config_file(tmp_path, supplied))
    assert store.get_profile('service') == original


def test_memory_policy_import_derives_target_binding_and_export_omits_internal_hash(store, tmp_path):
    policy = {'categories': ['procedure', 'lesson'], 'instance_ids': ['source-one'],
              'model_allowed': True, 'retain_allowed': True,
              'long_term_allowed': False, 'exports_allowed': False}
    config = {'name': 'service', 'resources': [resource()], 'memory_policy': policy}
    imported = import_profile(store, config_file(tmp_path, config))
    digest = hashlib.sha256(json.dumps(resource()['target'], ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')).encode()).hexdigest()
    assert imported['memory_policy'] == {**policy, 'target_bindings': {'source-one': digest}}
    exported = export_profile(store, 'service')
    assert exported['memory_policy'] == policy
    assert import_profile(store, config_file(tmp_path, exported))['memory_policy'] == imported['memory_policy']


def test_target_change_rebinds_internal_hash_without_granting_memory_permissions(store):
    configured = resource()
    policy = {'categories': ['lesson'], 'instance_ids': ['source-one'], 'model_allowed': False}
    original = store.put_profile('service', resources=[configured], memory_policy=policy)
    assert 'target_bindings' not in policy
    configured['target']['project'] = 'team/other'
    changed = store.put_profile('service', resources=[configured], memory_policy=original['memory_policy'])
    assert changed['memory_policy']['target_bindings'] != original['memory_policy']['target_bindings']
    assert changed['memory_policy']['model_allowed'] is False
    assert 'retain_allowed' not in changed['memory_policy']
    denied = store.put_profile('denied', resources=[configured], memory_policy={'model_allowed': False})
    assert denied['memory_policy']['model_allowed'] is False


@pytest.mark.parametrize('changes', [
    {'instance_ids': ['other']}, {'instance_ids': ['source-one', 'source-one']},
    {'target_bindings': {'source-one': 'wrong'}}, {'target_bindings': {'source-one': 1}},
    {'target_bindings': {'other': '0' * 64}},
])
def test_invalid_memory_targets_are_rejected_before_profile_publication(store, tmp_path, changes):
    policy = {'categories': ['lesson'], 'instance_ids': ['source-one'], **changes}
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        import_profile(store, config_file(tmp_path, {'name': 'service', 'resources': [resource()], 'memory_policy': policy}))
    assert store.list_profiles() == []


@pytest.mark.parametrize('document', [
    '{"name":"one","name":"two"}',
    '{"name":"service","retention_days":NaN}',
    '{"name":"service","retention_days":1e999}',
    '{"name":"service","unknown":true}',
    '{"name":"service","core_context":["password=PRIVATE_SENTINEL"]}',
])
def test_bad_profile_input_is_rejected_without_active_revision(store, tmp_path, document):
    path = tmp_path / 'input.json'
    path.write_text(document)
    with pytest.raises(StoreError):
        import_profile(store, path)
    assert store.list_profiles() == []


def test_import_size_limit_is_applied_before_parsing(store, tmp_path):
    path = tmp_path / 'oversized.json'
    path.write_bytes(b' ' * (2 * 1024 * 1024 + 1))
    with pytest.raises(StoreError):
        import_profile(store, path)


def test_plaintext_authentication_is_rejected_before_profile_write(store, tmp_path):
    config = resource()
    config['connection']['auth_env']['GITLAB_TOKEN'] = 'PRIVATE_SENTINEL'
    with pytest.raises(StoreError):
        import_profile(store, config_file(tmp_path, {'name': 'service', 'resources': [config]}))
    assert store.list_profiles() == []


def test_templates_use_catalog_pins_and_let_plans_define_data_permissions(store):
    catalog = load_catalog()
    assert len(catalog) == 6
    for recipe_id, item in catalog.items():
        template = profile_template(store, recipe_id, 'service', 'resource-one')
        assert template['retention_days'] is None
        assert template['long_term_memory_days'] is None
        configured = template['resources'][0]
        assert configured['policy'] is None
        assert configured['connection']['command'] == str(prepared_executable(store, recipe_id))
        assert configured['connection']['args'] == item['args']
        assert all(value.startswith('credential:') for value in configured['connection']['auth_env'].values())


def test_templates_use_product_specific_adapter_identifiers(store):
    expected = {
        'db-postgres': 'postgres_read', 'cache-redis': 'redis_read', 'infra-aws': 'aws_read',
        'source-gitlab': 'gitlab_read', 'document-confluence': 'confluence_read', 'issue-jira': 'jira_read',
    }
    for recipe, adapter_id in expected.items():
        template = profile_template(store, recipe, 'service', 'resource-one')
        assert template['resources'][0]['adapter'] == adapter_id


def test_template_uses_registered_adapter_target_without_sharing_mutable_values(store, monkeypatch):
    adapter = SimpleNamespace(adapter_id='postgres_read', resource_type='DB',
                              target_template={'host': 'SET-ME', 'port': 6000,
                                               'options': {'read_only': True}})
    monkeypatch.setattr('daeri.config.setup.get_adapter', lambda identifier: adapter)
    template = profile_template(store, 'db-postgres', 'service', 'database')['resources'][0]
    assert template['target'] == {'host': 'SET-ME', 'port': 6000, 'options': {'read_only': True}}
    template['target']['options']['read_only'] = False
    assert adapter.target_template['options']['read_only'] is True
    assert profile_template(store, 'db-postgres', 'service', 'database')['resources'][0]['target']['options']['read_only'] is True


@pytest.mark.parametrize('changes', [{'adapter': 'unsupported_read'}, {'type': 'Cache'}])
def test_template_rejects_unknown_adapter_or_wrong_resource_type(store, monkeypatch, changes):
    catalog = load_catalog()
    catalog['db-postgres'].update(changes)
    monkeypatch.setattr('daeri.config.setup.load_catalog', lambda: catalog)
    with pytest.raises(StoreError):
        profile_template(store, 'db-postgres', 'service', 'database')


def test_prepare_refuses_unselected_recipe_and_arbitrary_command(store):
    value = resource()
    store.put_profile('service', resources=[value])
    with pytest.raises(StoreError):
        prepare_resource(store, 'service', 'source-one', 'source-gitlab')
    with pytest.raises(StoreError):
        prepare_resource(store, 'service', 'source-one', '../../outside')


def prepared_resource(store):
    value = profile_template(store, 'source-gitlab', 'service', 'source-one')['resources'][0]
    value['target'] = {'server': 'https://git.example.test', 'project': 'team/service'}
    value['connection']['env']['GITLAB_URL'] = 'https://git.example.test'
    value['capabilities'][0]['constraints'] = {'fixed': {'project_id': 'team/service'},
                                              'allowed_values': {'ref': ['main']},
                                              'path_prefixes': {'file_path': ['src']}}
    store.put_profile('service', resources=[value])
    return value


def test_imported_managed_connection_can_be_prepared_and_diagnosed_locally(store, tmp_path, private_uv, monkeypatch):
    from contextlib import asynccontextmanager
    from mcp.types import ListToolsResult, Tool
    from daeri.infrastructure.resources import ResourceRegistry

    prepared_resource(store)
    exported = config_file(tmp_path, export_profile(store, 'service'))
    with closing(Store(tmp_path / 'new-pc')) as destination:
        imported = import_profile(destination, exported)
        command = Path(imported['resources'][0]['connection']['command'])
        assert command.is_relative_to(destination.data_dir)

        def run(args, **kwargs):
            if args[1] == 'venv':
                Path(args[-1]).mkdir(parents=True)
            elif args[1] == 'sync':
                environment = Path(kwargs['env']['UV_PROJECT_ENVIRONMENT'])
                (environment / 'bin').mkdir()
                (environment / 'bin/python').touch()
                (environment / 'bin/mcp-gitlab').touch()
            elif args[1:3] == ['-I', '-c']:
                return subprocess.CompletedProcess(args, 0, stdout='0.21.0\n', stderr='')
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

        monkeypatch.setattr('daeri.config.setup.subprocess.run', run)
        ready = prepare_resource(destination, 'service', 'source-one', 'source-gitlab')
        assert ready['ready'] is True and ready['reused'] is False
        assert ready['command'] == str(command) and command.is_file()

        @asynccontextmanager
        async def transport(parameters, **kwargs):
            assert parameters.command == str(command)
            assert parameters.env['GITLAB_URL'] == 'https://git.example.test'
            assert parameters.env['GITLAB_TOKEN'] == 'local-private'
            yield None, None

        class Session:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def initialize(self): pass
            async def list_tools(self, cursor=None):
                return ListToolsResult(tools=[Tool(name='gitlab_get_file', inputSchema={
                    'type': 'object', 'properties': {'project_id': {'type': 'string'},
                                                    'ref': {'type': 'string'}, 'file_path': {'type': 'string'}},
                    'required': ['project_id', 'ref', 'file_path'], 'additionalProperties': False})])

        monkeypatch.setattr('daeri.infrastructure.resources.stdio_client', transport)
        monkeypatch.setattr('daeri.infrastructure.resources.ClientSession', Session)
        registry = ResourceRegistry(destination, lambda ref: 'local-private')
        diagnosed = registry.diagnose(imported, 'source-one')
        assert diagnosed['ready'] is True and diagnosed['capability_count'] == 1
        descriptor = registry.get(imported, 'source-one', 'read-file')
        assert descriptor['scope_schema']['x-daeri-access-constraints'] == {
            'fixed': {'project_id': 'team/service'}, 'allowed_values': {'ref': ['main']},
            'path_prefixes': {'file_path': ['src']}}
        assert destination.get_profile('service') == imported


@pytest.mark.parametrize('adapter_id', ['source_read', 'gitlab_read'])
def test_prepare_uses_frozen_recipe_and_marks_only_verified_installation(store, private_uv, monkeypatch, adapter_id):
    configured = prepared_resource(store)
    configured['adapter'] = adapter_id
    store.put_profile('service', resources=[configured])
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if args[1] == 'venv':
            Path(args[-1]).mkdir(parents=True)
        elif args[1] == 'sync':
            candidate = Path(kwargs['env']['UV_PROJECT_ENVIRONMENT'])
            (candidate / 'bin').mkdir()
            (candidate / 'bin/python').touch()
            (candidate / 'bin/mcp-gitlab').touch()
        elif args[1:3] == ['-I', '-c']:
            return subprocess.CompletedProcess(args, 0, stdout='0.21.0\n', stderr='')
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
    monkeypatch.setattr('daeri.config.setup.subprocess.run', run)
    result = prepare_resource(store, 'service', 'source-one', 'source-gitlab')
    assert result['ready'] is True
    assert Path(result['command']).is_file()
    assert any('--frozen' in call for call in calls)
    count = len(calls)
    assert prepare_resource(store, 'service', 'source-one', 'source-gitlab')['reused'] is True
    assert len(calls) == count + 1
    profile = store.get_profile('service')
    assert profile['revision'] == 2
    assert profile['resources'][0]['adapter'] == adapter_id


def test_failed_prepare_never_changes_profile_or_publishes_program(store, private_uv, monkeypatch):
    configured = prepared_resource(store)
    revision = store.get_profile('service')['revision']
    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired('uv', 300)
    monkeypatch.setattr('daeri.config.setup.subprocess.run', failed)
    with pytest.raises(StoreError):
        prepare_resource(store, 'service', 'source-one', 'source-gitlab')
    assert store.get_profile('service')['revision'] == revision
    assert not Path(configured['connection']['command']).exists()


@pytest.mark.parametrize('changed', [{'port': 6380}, {'db': 1}, {'tls': False}])
def test_prepare_accepts_explicit_cache_connection_values(store, private_uv, tmp_path, monkeypatch, changed):
    from daeri.config.setup import configure_resource, validate_setup
    values = {'host': 'cache.example.invalid', 'port': 6379, 'db': 0, 'tls': True, **changed}
    path = config_file(tmp_path, values)
    assert validate_setup('cache-redis', path)['complete'] is True
    store.put_profile('service')
    configured = configure_resource(store, 'cache-redis', 'service', 'cache-one', path, expected_revision=1)

    def run(args, **kwargs):
        if args[1] == 'venv':
            Path(args[-1]).mkdir(parents=True)
        elif args[1] == 'sync':
            candidate = Path(kwargs['env']['UV_PROJECT_ENVIRONMENT'])
            program = candidate / ('Scripts' if os.name == 'nt' else 'bin')
            program.mkdir()
            suffix = '.exe' if os.name == 'nt' else ''
            (program / ('python' + suffix)).touch()
            (program / ('redis-mcp-server' + suffix)).touch()
        elif args[1:3] == ['-I', '-c']:
            return subprocess.CompletedProcess(args, 0, stdout='0.5.1\n', stderr='')
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

    monkeypatch.setattr('daeri.config.setup.subprocess.run', run)
    result = prepare_resource(store, 'service', 'cache-one', 'cache-redis')
    assert result['ready'] is True
    assert Path(result['command']).is_file()
    assert store.get_profile('service') == configured


def test_prepare_still_rejects_a_disabled_recipe_safety_setting(store, private_uv, monkeypatch):
    configured = prepared_resource(store)
    configured['connection']['env']['MCP_READ_ONLY'] = 'false'
    monkeypatch.setattr('daeri.config.setup.subprocess.run', lambda *args, **kwargs:
                        pytest.fail('Unsafe recipe settings must not start program preparation'))
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        store.put_profile('service', resources=[configured])


@pytest.fixture
def fake_preparation(private_uv, monkeypatch):
    catalog = load_catalog()
    failures = {}

    def run(args, **kwargs):
        if args[0] == str(private_uv):
            item = catalog[Path(args[args.index('--project') + 1]).name]
            if item['package'] in failures:
                raise failures.pop(item['package'])
            if args[1] == 'venv':
                Path(args[-1]).mkdir(parents=True)
            elif args[1] == 'sync':
                assert '--frozen' in args
                environment = Path(kwargs['env']['UV_PROJECT_ENVIRONMENT'])
                programs = environment / ('Scripts' if os.name == 'nt' else 'bin')
                programs.mkdir()
                suffix = '.exe' if os.name == 'nt' else ''
                (programs / ('python' + suffix)).touch()
                (programs / (item['executable'] + suffix)).touch()
            else:
                pytest.fail('Only registered recipe installation commands may run')
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        assert args[1:3] == ['-I', '-c']
        item = next(item for item in catalog.values() if item['package'] == args[-1])
        return subprocess.CompletedProcess(args, 0, stdout=item['version'] + '\n', stderr='')

    monkeypatch.setattr('daeri.config.setup.subprocess.run', run)
    return failures


def test_prepare_resources_derives_all_profile_instances_and_reuses_shared_recipes(store, fake_preparation):
    from daeri.config.setup import build_setup_resource
    selections = [
        ('db-postgres', 'primary', {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'}),
        ('cache-redis', 'sessions', {'host': 'cache.example.test', 'port': 6380, 'db': 1, 'tls': True}),
        ('infra-aws', 'cloud', {'profile': 'operations', 'region': 'ap-northeast-2', 'account': '111111111111'}),
        ('source-gitlab', 'repository', {'server': 'https://git.example.test'}),
        ('document-confluence', 'knowledge', {'server': 'https://docs.example.test/wiki', 'username': 'reader@example.test'}),
        ('issue-jira', 'tickets', {'server': 'https://issues.example.test', 'auth_type': 'personal_token'}),
        ('db-postgres', 'reference', {'host': 'reference.example.test', 'port': 5433, 'database': 'reference', 'username': 'reader'}),
    ]
    resources = [build_setup_resource(store, recipe, 'service', instance, values)
                 for recipe, instance, values in selections]
    resources[3]['adapter'] = 'source_read'
    profile = store.put_profile('service', resources=resources)

    result = setup.prepare_resources(store, 'service')

    assert result['ready'] is True
    assert [(item['instance_id'], item['recipe']) for item in result['resources']] == [
        ('primary', 'db-postgres'), ('sessions', 'cache-redis'), ('cloud', 'infra-aws'),
        ('repository', 'source-gitlab'), ('knowledge', 'document-confluence'),
        ('tickets', 'issue-jira'), ('reference', 'db-postgres')]
    assert [item['reused'] for item in result['resources']] == [False] * 6 + [True]
    assert all(item['ready'] and Path(item['command']).is_file() for item in result['resources'])
    for item in result['resources']:
        marker = Path(item['command']).parents[2] / 'prepared.json'
        assert json.loads(marker.read_text())['recipe'] == item['recipe']
    assert store.get_profile('service') == profile
    again = setup.prepare_resources(store, 'service')
    assert again['ready'] is True
    assert all(item['reused'] for item in again['resources'])
    assert store.get_profile('service') == profile


def test_prepare_resources_collects_installation_failure_and_continues(store, fake_preparation):
    from daeri.config.setup import build_setup_resource
    source = prepared_resource(store)
    cache = build_setup_resource(store, 'cache-redis', 'service', 'sessions',
                                 {'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True})
    profile = store.put_profile('service', resources=[source, cache])
    fake_preparation['mcp-gitlab'] = subprocess.TimeoutExpired('PRIVATE_SENTINEL', 300)

    result = setup.prepare_resources(store, 'service')

    assert result['ready'] is False
    assert result['resources'][0] == {'instance_id': 'source-one', 'recipe': 'source-gitlab',
                                      'ready': False, 'error': {'code': 'CONNECTION_NOT_READY'}}
    assert result['resources'][1]['instance_id'] == 'sessions'
    assert result['resources'][1]['ready'] is True
    assert Path(result['resources'][1]['command']).is_file()
    assert not Path(source['connection']['command']).exists()
    assert not list((store.data_dir / 'tools/source-gitlab').glob('.prepare*'))
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)
    assert store.get_profile('service') == profile


def test_prepare_resources_preserves_obstructing_file_and_prepares_other_connections(store, fake_preparation):
    from daeri.config.setup import build_setup_resource
    source = prepared_resource(store)
    cache = build_setup_resource(store, 'cache-redis', 'service', 'sessions',
                                 {'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True})
    profile = store.put_profile('service', resources=[source, cache])
    obstruction = store.data_dir / 'tools/source-gitlab'
    obstruction.parent.mkdir()
    obstruction.write_text('Keep this existing file.\n')

    result = setup.prepare_resources(store, 'service')

    assert result['ready'] is False
    assert result['resources'][0] == {'instance_id': 'source-one', 'recipe': 'source-gitlab',
                                      'ready': False, 'error': {'code': 'CONNECTION_NOT_READY'}}
    assert result['resources'][1]['instance_id'] == 'sessions'
    assert result['resources'][1]['ready'] is True
    assert Path(result['resources'][1]['command']).is_file()
    assert obstruction.read_text() == 'Keep this existing file.\n'
    assert store.get_profile('service') == profile


@pytest.mark.parametrize('mismatch', ['command', 'args', 'fixed-env'])
def test_prepare_resources_rejects_connections_outside_registered_recipe(store, monkeypatch, mismatch):
    from daeri.config.setup import build_setup_resource
    if mismatch == 'args':
        configured = build_setup_resource(store, 'db-postgres', 'service', 'primary',
                                          {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'})
        configured['connection']['args'] = ['--access-mode', 'restricted']
    else:
        configured = prepared_resource(store)
        if mismatch == 'command':
            configured['connection']['command'] = sys.executable
        else:
            configured['connection']['env'].pop('GITLAB_SSL_VERIFY')
    profile = store.put_profile('service', resources=[configured])
    monkeypatch.setattr('daeri.config.setup.subprocess.run', lambda *args, **kwargs:
                        pytest.fail('Unmatched or unsafe connections must not execute a command'))

    result = setup.prepare_resources(store, 'service')

    assert result == {'ready': False, 'resources': [{
        'instance_id': configured['instance_id'],
        'recipe': 'source-gitlab' if mismatch == 'fixed-env' else None,
        'ready': False, 'error': {'code': 'INVALID_INPUT'}}]}
    assert store.get_profile('service') == profile
    assert not (store.data_dir / 'tools').exists()


def test_prepare_resources_accepts_an_empty_profile_without_starting_installation(store, monkeypatch):
    profile = store.put_profile('service')
    monkeypatch.setattr('daeri.config.setup.subprocess.run', lambda *args, **kwargs:
                        pytest.fail('An empty profile has no program to prepare'))
    assert setup.prepare_resources(store, 'service') == {'ready': True, 'resources': []}
    assert store.get_profile('service') == profile


@pytest.mark.parametrize('fail_first', [False, True])
def test_resource_prepare_profile_cli_prepares_saved_connections(store, fake_preparation, monkeypatch, capsys, fail_first):
    from daeri.cli import main
    from daeri.config.setup import build_setup_resource
    source = prepared_resource(store)
    cache = build_setup_resource(store, 'cache-redis', 'service', 'sessions',
                                 {'host': 'cache.example.test', 'port': 6379, 'db': 0, 'tls': True})
    profile = store.put_profile('service', resources=[source, cache])
    if fail_first:
        fake_preparation['mcp-gitlab'] = subprocess.TimeoutExpired('PRIVATE_SENTINEL', 300)
    monkeypatch.setattr(sys, 'argv', ['daeri', '--data-dir', str(store.data_dir), 'resource', 'prepare', 'service'])

    assert main() == (1 if fail_first else 0)

    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert captured.err == ''
    assert response['ready'] is not fail_first
    assert [item['instance_id'] for item in response['resources']] == ['source-one', 'sessions']
    assert response['resources'][1]['ready'] is True
    assert Path(response['resources'][1]['command']).is_file()
    if fail_first:
        failure = response['resources'][0]
        assert failure['ready'] is False
        assert failure['error']['code'] == 'CONNECTION_NOT_READY'
        assert failure['error']['stage'] == 'preparation'
        assert failure['error']['next_action'] == 'inspect_preparation'
    assert 'PRIVATE_SENTINEL' not in captured.out
    assert store.get_profile('service') == profile


@pytest.mark.parametrize('problem', ['missing-python', 'missing-executable', 'wrong-version'])
def test_resource_verification_reports_an_unready_installation(tmp_path, monkeypatch, problem):
    environment = tmp_path / 'tool'
    programs = environment / ('Scripts' if os.name == 'nt' else 'bin')
    programs.mkdir(parents=True)
    suffix = '.exe' if os.name == 'nt' else ''
    if problem != 'missing-python':
        (programs / ('python' + suffix)).touch()
    if problem != 'missing-executable':
        (programs / ('probe' + suffix)).touch()
    def run(args, **kwargs):
        assert problem == 'wrong-version'
        return subprocess.CompletedProcess(args, 0, stdout='0.9\n', stderr='')
    monkeypatch.setattr('daeri.config.setup.subprocess.run', run)

    with pytest.raises(StoreError) as caught:
        setup._verify(environment, {'executable': 'probe', 'package': 'probe', 'version': '1.0'})
    assert caught.value.code == 'CONNECTION_NOT_READY'


@pytest.mark.parametrize('invalid_response', [None, 'prompt-profile', 'result-account', 'extra-field'])
def test_login_forwards_only_public_device_authorization_and_keeps_profile_revision(store, monkeypatch, invalid_response):
    from daeri.config.setup import login_resource
    value = profile_template(store, 'infra-aws', 'service', 'infra-one')['resources'][0]
    value['target'] = {'profile': 'operations', 'region': 'ap-northeast-2', 'account': '111111111111'}
    value['connection']['env'].update(AWS_PROFILE='operations', AWS_REGION='ap-northeast-2')
    value['capabilities'][0]['constraints']['region'] = 'ap-northeast-2'
    store.put_profile('service', resources=[value])
    real_popen = subprocess.Popen
    child = """import json,os,sys
assert sys.flags.isolated
assert 'PYTHONHOME' not in os.environ and 'UV_PROJECT_ENVIRONMENT' not in os.environ
request=json.load(sys.stdin)
prompt={'provider':'infra_sso','verification_uri':'https://device.sso.ap-northeast-2.amazonaws.com/','user_code':'ABCD-EFGH','provider_profile':request['profile']}
completion={'authenticated':True,'provider_profile':request['profile'],'account':request['account'],'role':'Reader','expires_at':'2099-01-01T00:00:00Z'}
""" + f"invalid={invalid_response!r}\n" + """
if invalid=='prompt-profile': prompt['provider_profile']='different-profile'
if invalid=='result-account': completion['account']='222222222222'
if invalid=='extra-field': prompt['access_token']='PRIVATE_SENTINEL'
print(json.dumps({'status':'pending','data':prompt,'error':None}),flush=True)
print(json.dumps({'status':'ok','data':completion,'error':None}),flush=True)
"""
    monkeypatch.setenv('PYTHONHOME', '/unrelated-python')
    monkeypatch.setenv('UV_PROJECT_ENVIRONMENT', '/unrelated-environment')
    monkeypatch.setattr('daeri.config.setup.subprocess.Popen', lambda args, **kwargs:
                        real_popen([sys.executable, *args[1:-1], '-c', child], **kwargs))
    monkeypatch.setattr('daeri.config.setup._verify', lambda environment, item: None)
    events = []
    if invalid_response is None:
        result = login_resource(store, 'service', 'infra-one', role='Reader', emit=events.append)
        assert result['authenticated'] is True
        assert events[0]['user_code'] == 'ABCD-EFGH'
    else:
        with pytest.raises(StoreError) as caught:
            login_resource(store, 'service', 'infra-one', role='Reader', emit=events.append)
        assert caught.value.code == 'INVALID_INPUT'
        assert 'PRIVATE_SENTINEL' not in str(caught.value)
        if invalid_response != 'result-account':
            assert events == []
    assert store.get_profile('service')['revision'] == 1


def test_login_rejects_adapters_without_a_registered_login_hook(store, monkeypatch):
    from daeri.config.setup import login_resource
    prepared_resource(store)
    monkeypatch.setattr('daeri.config.setup._verify', lambda *args: pytest.fail('Unsupported login must not inspect or start a prepared program'))
    with pytest.raises(StoreError) as caught:
        login_resource(store, 'service', 'source-one')
    assert caught.value.code == 'INVALID_INPUT'


def test_preparation_child_drops_program_overrides_but_keeps_explicit_user_context(tmp_path, monkeypatch):
    from daeri.config.setup import _run
    overrides = {
        'PYTHONPATH': str(tmp_path / 'foreign-packages'),
        'PYTHONHOME': str(tmp_path / 'foreign-python'),
        'UV_PROJECT_ENVIRONMENT': str(tmp_path / 'foreign-environment'),
        'UV_CONFIG_FILE': str(tmp_path / 'foreign-uv.toml'),
        'VIRTUAL_ENV': str(tmp_path / 'foreign-venv'),
        'CONDA_PREFIX': str(tmp_path / 'foreign-conda'),
        '__PYVENV_LAUNCHER__': str(tmp_path / 'foreign-launcher'),
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('DAERI_DATA_DIR', str(tmp_path / 'private-data'))
    monkeypatch.setenv('SSL_CERT_FILE', str(tmp_path / 'company-ca.pem'))
    result = _run([sys.executable, '-I', '-c',
                   'import json,os,sys; print(json.dumps({key: os.environ[key] '
                   'for key in sys.argv[1:] if key in os.environ}))',
                   *overrides, 'DAERI_DATA_DIR', 'SSL_CERT_FILE'])
    received = json.loads(result.stdout)
    assert not set(overrides) & received.keys()
    assert received['DAERI_DATA_DIR'] == str(tmp_path / 'private-data')
    assert received['SSL_CERT_FILE'] == str(tmp_path / 'company-ca.pem')
    assert all(os.environ[key] == value for key, value in overrides.items())


def test_resource_verification_ignores_current_directory_imports(tmp_path, monkeypatch):
    from daeri.config.setup import _verify
    import venv
    environment = tmp_path / 'tool'
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    library = subprocess.run([str(python), '-I', '-c',
                              'import sysconfig; print(sysconfig.get_path("purelib"))'],
                             text=True, capture_output=True, check=True).stdout.strip()
    metadata = Path(library) / 'daeri_test_probe-1.0.dist-info'
    metadata.mkdir(parents=True)
    (metadata / 'METADATA').write_text('Name: daeri-test-probe\nVersion: 1.0\n')
    executable = environment / ('Scripts/probe.exe' if os.name == 'nt' else 'bin/probe')
    executable.touch()
    poison = tmp_path / 'unrelated-project'
    poison.mkdir()
    (poison / 'pathlib.py').write_text('raise RuntimeError("Foreign project import")\n')
    monkeypatch.chdir(poison)
    monkeypatch.delenv('PYTHONPATH', raising=False)
    assert _verify(environment, {'executable': 'probe', 'package': 'daeri-test-probe',
                                 'version': '1.0'}) == executable


def test_resource_prepare_requires_private_uv_even_when_global_uv_exists(store, tmp_path, monkeypatch):
    prepared_resource(store)
    commands = tmp_path / 'global-tools'
    commands.mkdir()
    marker = commands / 'global-uv-used'
    executable = commands / ('uv.exe' if os.name == 'nt' else 'uv')
    if os.name == 'nt':
        pytest.skip('Native Windows executable fixture requires Windows bootstrap validation')
    executable.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(commands))
    monkeypatch.setattr(sys, 'prefix', str(tmp_path / 'unprepared-plugin' / '.venv'))
    with pytest.raises(StoreError) as caught:
        prepare_resource(store, 'service', 'source-one', 'source-gitlab')
    assert not marker.exists()
    assert caught.value.code == 'CONNECTION_NOT_READY'
