import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from daeri.setup import (export_profile, import_profile, load_catalog, profile_template,
                         prepare_resource, prepared_executable)
from daeri.storage import Store, StoreError


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'private')
    yield value
    value.close()


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
    assert export_profile(store, 'service') == config
    assert import_profile(store, config_file(tmp_path, config))['revision'] == 2


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
        configured = template['resources'][0]
        assert configured['policy'] == {}
        assert configured['connection']['command'] == str(prepared_executable(store, recipe_id))
        assert configured['connection']['args'] == item['args']
        assert all(value.startswith('credential:') for value in configured['connection']['auth_env'].values())


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


def test_prepare_uses_frozen_recipe_and_marks_only_verified_installation(store, monkeypatch):
    prepared_resource(store)
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
        elif args[1] == '-c':
            return subprocess.CompletedProcess(args, 0, stdout='0.21.0\n', stderr='')
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
    monkeypatch.setattr('daeri.setup.subprocess.run', run)
    monkeypatch.setattr('daeri.setup.shutil.which', lambda _: '/verified/uv')
    result = prepare_resource(store, 'service', 'source-one', 'source-gitlab')
    assert result['ready'] is True
    assert Path(result['command']).is_file()
    assert any('--frozen' in call for call in calls)
    count = len(calls)
    assert prepare_resource(store, 'service', 'source-one', 'source-gitlab')['reused'] is True
    assert len(calls) == count + 1


def test_failed_prepare_never_changes_profile_or_publishes_program(store, monkeypatch):
    configured = prepared_resource(store)
    revision = store.get_profile('service')['revision']
    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired('uv', 300)
    monkeypatch.setattr('daeri.setup.subprocess.run', failed)
    monkeypatch.setattr('daeri.setup.shutil.which', lambda _: '/verified/uv')
    with pytest.raises(StoreError):
        prepare_resource(store, 'service', 'source-one', 'source-gitlab')
    assert store.get_profile('service')['revision'] == revision
    assert not Path(configured['connection']['command']).exists()


def test_login_forwards_only_public_device_authorization_and_keeps_profile_revision(store, monkeypatch):
    from daeri.setup import login_resource
    value = profile_template(store, 'infra-aws', 'service', 'infra-one')['resources'][0]
    value['target'] = {'profile': 'operations', 'region': 'ap-northeast-2', 'account': '111111111111'}
    value['connection']['env'].update(AWS_PROFILE='operations', AWS_REGION='ap-northeast-2')
    value['capabilities'][0]['constraints']['region'] = 'ap-northeast-2'
    store.put_profile('service', resources=[value])
    real_popen = subprocess.Popen
    child = """import json,sys
request=json.load(sys.stdin)
print(json.dumps({'status':'pending','data':{'provider':'infra_sso','verification_uri':'https://device.sso.ap-northeast-2.amazonaws.com/','user_code':'ABCD-EFGH','provider_profile':request['profile']},'error':None}),flush=True)
print(json.dumps({'status':'ok','data':{'authenticated':True,'provider_profile':request['profile'],'account':request['account'],'role':'Reader','expires_at':'2099-01-01T00:00:00Z'},'error':None}),flush=True)
"""
    monkeypatch.setattr('daeri.setup.subprocess.Popen', lambda args, **kwargs: real_popen([sys.executable, '-c', child], **kwargs))
    monkeypatch.setattr('daeri.setup._verify', lambda environment, item: None)
    events = []
    result = login_resource(store, 'service', 'infra-one', role='Reader', emit=events.append)
    assert result['authenticated'] is True
    assert events[0]['user_code'] == 'ABCD-EFGH'
    assert store.get_profile('service')['revision'] == 1
