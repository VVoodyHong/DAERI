"""Operator catalog publishing validates every payload before creating AWS clients."""

from copy import deepcopy
import importlib
import json

import boto3
import pytest
from yaml import safe_dump


GATEWAY = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/payments-abcdefghij'
ROLE = 'arn:aws:iam::123456789012:role/connector-runtime'
IMAGE = '123456789012.dkr.ecr.us-east-1.amazonaws.com/http-mcp:stable'
CONFIG = {
    'region': 'us-east-1',
    'prefix': '/daeri/connections/',
    'connectors': {
        'reports': {'name': 'Reports', 'kind': 'runtime', 'image_uri': IMAGE,
                    'command': ['/opt/bin/http-mcp'],
                    'environment': {'API_HOST': 'https://internal.example.test'},
                    'inputs': {'token': {'title': 'API token', 'env': 'API_TOKEN', 'secret_ref': True}}},
    },
    'projects': {'payments': {'gateway_arn': GATEWAY, 'allowed_connectors': ['reports'], 'runtime_role_arn': ROLE}},
}


def command():
    return importlib.import_module('scripts.configure_connections')


def write_config(tmp_path, config):
    path = tmp_path / 'connections.yaml'
    path.write_text(safe_dump(config))
    return path


def test_catalog_check_returns_only_ids_without_initializing_aws(tmp_path, monkeypatch, capsys):
    module = command()
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: pytest.fail('Dry run initialized AWS clients'))
    monkeypatch.setattr(boto3, 'Session', lambda *args, **kw: pytest.fail('Dry run initialized an AWS session'))
    assert module.main(['--config', str(write_config(tmp_path, CONFIG))]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {'applied': False, 'connectors': ['reports'], 'projects': ['payments']}
    assert not output.err
    assert IMAGE not in output.out and GATEWAY not in output.out and 'internal.example' not in output.out


def test_catalog_apply_publishes_typed_reference_documents_and_closes_client(tmp_path, monkeypatch, capsys):
    module = command()
    events = []
    class SSM:
        def __enter__(self):
            events.append('open')
            return self
        def __exit__(self, *args):
            events.append('close')
        def get_parameters_by_path(self, **kwargs):
            assert kwargs == {'Path': '/daeri/connections/projects/', 'Recursive': False,
                              'WithDecryption': False, 'MaxResults': 10}
            return {'Parameters': []}
        def put_parameter(self, **kwargs):
            events.append(kwargs)
            return {'Version': 2}
    monkeypatch.setattr(boto3, 'client', lambda service, **kw: SSM() if service == 'ssm' else pytest.fail(service))
    assert module.main(['--config', str(write_config(tmp_path, CONFIG)), '--apply']) == 0
    assert events[0] == 'open' and events[-1] == 'close'
    templates, projects = events[1:-1]
    assert templates['Name'] == '/daeri/connections/catalog/reports'
    assert projects['Name'] == '/daeri/connections/projects/payments'
    assert templates['Type'] == projects['Type'] == 'String'
    assert templates['Overwrite'] is projects['Overwrite'] is True
    assert templates['Tier'] == projects['Tier'] == 'Intelligent-Tiering'
    template, project = json.loads(templates['Value']), json.loads(projects['Value'])
    assert template['image_uri'] == IMAGE and template['command'] == ['/opt/bin/http-mcp']
    assert template['inputs']['token']['secret_ref'] is True
    assert project['gateway_arn'] == GATEWAY and project['allowed_connectors'] == ['reports']
    output = capsys.readouterr()
    assert json.loads(output.out) == {'applied': True, 'connectors': ['reports'], 'projects': ['payments']}
    assert not output.err
    assert IMAGE not in output.out and GATEWAY not in output.out


@pytest.mark.parametrize('apply', [False, True])
@pytest.mark.parametrize('failure', ['duplicate_yaml', 'unknown_field', 'bad_id', 'bad_prefix', 'bad_template', 'oversized_last_payload'])
def test_catalog_invalid_input_never_initializes_aws(tmp_path, monkeypatch, capsys, apply, failure):
    module = command()
    config = deepcopy(CONFIG)
    if failure == 'unknown_field':
        config['private-key'] = 'private-value'
    elif failure == 'bad_id':
        config['connectors']['../private-value'] = config['connectors'].pop('reports')
    elif failure == 'bad_prefix':
        config['prefix'] = '/daeri/../private-value/'
    elif failure == 'bad_template':
        config['connectors']['reports']['command'] = 'private-value'
    elif failure == 'oversized_last_payload':
        config['connectors']['z-last'] = {'name': 'Huge', 'kind': 'runtime', 'image_uri': IMAGE,
                                          'command': ['http-mcp'], 'environment': {'HOST': 'private-value' * 1000}}
    path = write_config(tmp_path, config)
    if failure == 'duplicate_yaml':
        path.write_text('private-key: private-value\nprivate-key: duplicate\n')
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: pytest.fail('Invalid input initialized AWS clients'))
    monkeypatch.setattr(boto3, 'Session', lambda *args, **kw: pytest.fail('Invalid input initialized an AWS session'))
    assert module.main(['--config', str(path)] + (['--apply'] if apply else [])) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)['error']['code'] == 'INVALID_INPUT'
    assert not output.out
    assert 'private-key' not in output.err and 'private-value' not in output.err


def test_catalog_accepts_payloads_larger_than_standard_parameter_limit(tmp_path, monkeypatch):
    module = command()
    config = deepcopy(CONFIG)
    config['connectors']['reports']['environment']['API_HOST'] = 'a' * 4500
    written = []
    class SSM:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_parameters_by_path(self, **kwargs):
            return {'Parameters': []}
        def put_parameter(self, **kwargs):
            written.append(kwargs)
            return {'Version': 1}
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: SSM())
    assert module.main(['--config', str(write_config(tmp_path, config)), '--apply']) == 0
    assert 4096 < len(written[0]['Value'].encode()) <= 8192
    assert written[0]['Tier'] == 'Intelligent-Tiering'


def test_catalog_aws_failure_is_sanitized_and_closes_client(tmp_path, monkeypatch, capsys):
    module = command()
    events = []
    class SSM:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            events.append('close')
        def get_parameters_by_path(self, **kwargs):
            return {'Parameters': []}
        def put_parameter(self, **kwargs):
            raise RuntimeError('private-value ' + GATEWAY)
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: SSM())
    assert module.main(['--config', str(write_config(tmp_path, CONFIG)), '--apply']) == 2
    assert events == ['close']
    output = capsys.readouterr()
    assert json.loads(output.err)['error']['code'] == 'DEPLOYMENT_FAILED'
    assert not output.out
    assert 'private-value' not in output.err and GATEWAY not in output.err


@pytest.mark.parametrize('duplicate', ['gateway', 'secret_prefix'])
def test_catalog_rejects_shared_project_resources_before_aws(tmp_path, monkeypatch, capsys, duplicate):
    module = command()
    config = deepcopy(CONFIG)
    other = deepcopy(config['projects']['payments'])
    if duplicate == 'secret_prefix':
        config['projects']['payments']['secret_arn_prefix'] = (
            'arn:aws:secretsmanager:us-east-1:123456789012:secret:payments/')
        other['secret_arn_prefix'] = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:payments/database'
        other['gateway_arn'] = GATEWAY.replace('payments-', 'other-')
    config['projects']['other'] = other
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: pytest.fail('Shared project resources initialized AWS'))
    assert module.main(['--config', str(write_config(tmp_path, config)), '--apply']) == 2
    assert json.loads(capsys.readouterr().err)['error']['code'] == 'INVALID_INPUT'


def test_catalog_existing_bindings_are_paginated_before_any_writes(tmp_path, monkeypatch, capsys):
    module = command()
    requests, writes = [], []
    class SSM:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_parameters_by_path(self, **kwargs):
            requests.append(kwargs)
            if 'NextToken' not in kwargs:
                return {'Parameters': [], 'NextToken': 'second-page'}
            return {'Parameters': [{'Name': '/daeri/connections/projects/other',
                                     'Value': json.dumps(CONFIG['projects']['payments'])}]}
        def put_parameter(self, **kwargs):
            writes.append(kwargs)
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: SSM())
    assert module.main(['--config', str(write_config(tmp_path, CONFIG)), '--apply']) == 2
    assert requests[1]['NextToken'] == 'second-page' and not writes
    output = capsys.readouterr()
    assert json.loads(output.err)['error']['code'] == 'INVALID_INPUT'
    assert GATEWAY not in output.err and not output.out


@pytest.mark.parametrize('previous', [json.dumps(CONFIG['projects']['payments']), 'private-invalid-json'])
def test_catalog_allows_operators_to_replace_the_existing_project_binding(tmp_path, monkeypatch, previous):
    module = command()
    written = []
    class SSM:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_parameters_by_path(self, **kwargs):
            return {'Parameters': [{'Name': '/daeri/connections/projects/payments',
                                    'Value': previous}]}
        def put_parameter(self, **kwargs):
            written.append(kwargs)
            return {'Version': 1}
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: SSM())
    assert module.main(['--config', str(write_config(tmp_path, CONFIG)), '--apply']) == 0
    assert len(written) == 2
