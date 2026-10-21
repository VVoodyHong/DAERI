import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from daeri.setup import (export_profile, import_profile, load_catalog, profile_template,
                         prepare_resource, prepared_executable)
from daeri.storage import Store, StoreError


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
            'policy': {}}


def config_file(tmp_path, value):
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(value))
    return path


def test_import_and_export_keep_only_portable_configuration(store, tmp_path):
    config = {'name': 'service', 'core_context': ['UTC 운영'], 'resources': [resource()],
              'retention_days': 15, 'long_term_memory_days': 90, 'memory_policy': {}}
    result = import_profile(store, config_file(tmp_path, config))
    assert result['revision'] == 1
    assert export_profile(store, 'service') == {**config, 'embedding': {'provider': 'local'}}
    assert import_profile(store, config_file(tmp_path, config))['revision'] == 2


def test_unlimited_memory_retention_round_trips_without_replacing_other_settings(store, tmp_path):
    config = {'name': 'service', 'core_context': ['UTC 운영'], 'resources': [resource()],
              'retention_days': 15, 'long_term_memory_days': None, 'memory_policy': {}}
    profile = import_profile(store, config_file(tmp_path, config))
    assert profile['long_term_memory_days'] is None
    assert export_profile(store, 'service') == {**config, 'embedding': {'provider': 'local'}}
    assert import_profile(store, config_file(tmp_path, export_profile(store, 'service')))['retention_days'] == 15


def test_all_retention_limits_can_be_unlimited_in_profile_import_export(store, tmp_path):
    config = {'name': 'service', 'core_context': ['UTC 운영'], 'resources': [resource()],
              'retention_days': None, 'long_term_memory_days': None, 'memory_policy': {}}
    profile = import_profile(store, config_file(tmp_path, config))
    assert profile['retention_days'] is None
    assert export_profile(store, 'service') == {**config, 'embedding': {'provider': 'local'}}


@pytest.mark.parametrize('field', ['core_context', 'resources', 'memory_policy', 'embedding'])
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
    assert store.put_profile('denied', resources=[configured])['memory_policy'] == {}


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


def test_templates_use_only_catalog_pins_and_deny_data_by_default(store):
    catalog = load_catalog()
    assert len(catalog) == 5
    for recipe_id, item in catalog.items():
        template = profile_template(store, recipe_id, 'service', 'resource-one')
        assert template['retention_days'] is None
        assert template['long_term_memory_days'] is None
        configured = template['resources'][0]
        assert configured['policy'] == {}
        assert configured['connection']['command'] == str(prepared_executable(store, recipe_id))
        assert configured['connection']['args'] == item['args']
        assert all(value.startswith('credential:') for value in configured['connection']['auth_env'].values())


def test_templates_use_product_specific_adapter_identifiers(store):
    expected = {
        'db-postgres': 'postgres_read', 'cache-redis': 'redis_read', 'infra-aws': 'aws_read',
        'source-gitlab': 'gitlab_read', 'document-confluence': 'confluence_read',
    }
    for recipe, adapter_id in expected.items():
        template = profile_template(store, recipe, 'service', 'resource-one')
        assert template['resources'][0]['adapter'] == adapter_id


def test_template_uses_registered_adapter_target_without_sharing_mutable_values(store, monkeypatch):
    adapter = SimpleNamespace(adapter_id='postgres_read', resource_type='DB',
                              target_template={'host': 'SET-ME', 'port': 6000,
                                               'options': {'read_only': True}})
    monkeypatch.setattr('daeri.setup.get_adapter', lambda identifier: adapter)
    template = profile_template(store, 'db-postgres', 'service', 'database')['resources'][0]
    assert template['target'] == {'host': 'SET-ME', 'port': 6000, 'options': {'read_only': True}}
    template['target']['options']['read_only'] = False
    assert adapter.target_template['options']['read_only'] is True
    assert profile_template(store, 'db-postgres', 'service', 'database')['resources'][0]['target']['options']['read_only'] is True


@pytest.mark.parametrize('changes', [{'adapter': 'unsupported_read'}, {'type': 'Cache'}])
def test_template_rejects_unknown_adapter_or_wrong_resource_type(store, monkeypatch, changes):
    catalog = load_catalog()
    catalog['db-postgres'].update(changes)
    monkeypatch.setattr('daeri.setup.load_catalog', lambda: catalog)
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
    monkeypatch.setattr('daeri.setup.subprocess.run', run)
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
    monkeypatch.setattr('daeri.setup.subprocess.run', failed)
    with pytest.raises(StoreError):
        prepare_resource(store, 'service', 'source-one', 'source-gitlab')
    assert store.get_profile('service')['revision'] == revision
    assert not Path(configured['connection']['command']).exists()


@pytest.mark.parametrize('changed', [{'port': 6380}, {'db': 1}, {'tls': False}])
def test_prepare_accepts_explicit_cache_connection_values(store, private_uv, tmp_path, monkeypatch, changed):
    from daeri.setup import configure_resource, validate_setup
    values = {'host': 'cache.example.invalid', 'port': 6379, 'db': 0, 'tls': True,
              'keys': ['service:status'], **changed}
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

    monkeypatch.setattr('daeri.setup.subprocess.run', run)
    result = prepare_resource(store, 'service', 'cache-one', 'cache-redis')
    assert result['ready'] is True
    assert Path(result['command']).is_file()
    assert store.get_profile('service') == configured


def test_prepare_still_rejects_a_disabled_recipe_safety_setting(store, private_uv, monkeypatch):
    configured = prepared_resource(store)
    configured['connection']['env']['MCP_READ_ONLY'] = 'false'
    store.put_profile('service', resources=[configured])
    monkeypatch.setattr('daeri.setup.subprocess.run', lambda *args, **kwargs:
                        pytest.fail('Unsafe recipe settings must not start program preparation'))
    with pytest.raises(StoreError) as caught:
        prepare_resource(store, 'service', 'source-one', 'source-gitlab')
    assert caught.value.code == 'INVALID_INPUT'


@pytest.mark.parametrize('invalid_response', [None, 'prompt-profile', 'result-account', 'extra-field'])
def test_login_forwards_only_public_device_authorization_and_keeps_profile_revision(store, monkeypatch, invalid_response):
    from daeri.setup import login_resource
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
    monkeypatch.setattr('daeri.setup.subprocess.Popen', lambda args, **kwargs:
                        real_popen([sys.executable, *args[1:-1], '-c', child], **kwargs))
    monkeypatch.setattr('daeri.setup._verify', lambda environment, item: None)
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
    from daeri.setup import login_resource
    prepared_resource(store)
    monkeypatch.setattr('daeri.setup._verify', lambda *args: pytest.fail('Unsupported login must not inspect or start a prepared program'))
    with pytest.raises(StoreError) as caught:
        login_resource(store, 'service', 'source-one')
    assert caught.value.code == 'INVALID_INPUT'


def test_preparation_child_drops_program_overrides_but_keeps_explicit_user_context(tmp_path, monkeypatch):
    from daeri.setup import _run
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
    from daeri.setup import _verify
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
