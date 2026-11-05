"""The connector launcher resolves references only into its child process."""

import importlib
import json
import os
import boto3
import pytest


ARN = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:connector-token-AbCdEf'


def launcher():
    return importlib.import_module('daeri.infrastructure.connector_cli')


@pytest.mark.parametrize('arn', [ARN, 'arn:aws:secretsmanager:us-east-1:123456789012:secret:' + 'a' * 512 + '-AbCdEf'])
def test_launcher_resolves_secrets_only_for_child_and_closes_client_before_exec(monkeypatch, capsys, arn):
    module = launcher()
    monkeypatch.setenv('DAERI_CONNECTOR_CONFIG', json.dumps({
        'command': ['/opt/bin/http-mcp', '--label', 'literal $(value)'],
        'environment': {'ENDPOINT': 'https://internal.example.test'},
        'secrets': {'API_TOKEN': arn},
    }))
    monkeypatch.setenv('EXISTING', 'retained')
    monkeypatch.delenv('API_TOKEN', raising=False)
    events = []
    class Secrets:
        def __enter__(self):
            events.append('open')
            return self
        def __exit__(self, *args):
            events.append('close')
        def get_secret_value(self, **kwargs):
            assert kwargs == {'SecretId': arn}
            return {'SecretString': 'private-token', 'ARN': ARN}
    monkeypatch.setattr(boto3, 'client', lambda name, **kw: Secrets() if name == 'secretsmanager' else pytest.fail(name))
    def execute(file, argv, environment):
        events.append((file, argv, environment))
        assert 'API_TOKEN' not in os.environ
    monkeypatch.setattr(module.os, 'execvpe', execute)
    assert module.main([]) == 0
    assert events[:2] == ['open', 'close']
    file, argv, environment = events[2]
    assert file == '/opt/bin/http-mcp'
    assert argv == ['/opt/bin/http-mcp', '--label', 'literal $(value)']
    assert environment['API_TOKEN'] == 'private-token'
    assert environment['ENDPOINT'] == 'https://internal.example.test'
    assert environment['EXISTING'] == 'retained'
    assert 'DAERI_CONNECTOR_CONFIG' not in environment
    assert not capsys.readouterr().out


def test_launcher_without_secret_references_never_initializes_aws(monkeypatch):
    module = launcher()
    monkeypatch.setenv('DAERI_CONNECTOR_CONFIG', '{"command":["http-mcp"],"environment":{},"secrets":{}}')
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: pytest.fail('AWS initialized without references'))
    called = []
    monkeypatch.setattr(module.os, 'execvpe', lambda *args: called.append(args))
    assert module.main([]) == 0
    assert called[0][:2] == ('http-mcp', ['http-mcp'])


@pytest.mark.parametrize('config', [
    None, '', 'private malformed json', '[]',
    '{"command":["http-mcp"],"command":["other"]}',
    {'command': []}, {'command': 'http-mcp'}, {'command': ['']}, {'command': [1]},
    {'command': ['http-mcp\x00bad']}, {'command': ['http-mcp'], 'private-key': 'private-value'},
    {'command': ['http-mcp'], 'environment': {'BAD=NAME': 'private-value'}},
    {'command': ['http-mcp'], 'environment': {'1BAD': 'private-value'}},
    {'command': ['http-mcp'], 'environment': {'TOKEN': 1}},
    {'command': ['http-mcp'], 'environment': {'TOKEN': 'private\x00value'}},
    {'command': ['http-mcp'], 'environment': {'DAERI_CONNECTOR_CONFIG': 'private-value'}},
    {'command': ['http-mcp'], 'secrets': {'TOKEN': 'private-value'}},
    {'command': ['http-mcp'], 'secrets': {'TOKEN': ARN.replace(':aws:', ':private:')}},
    {'command': ['http-mcp'], 'secrets': {'TOKEN': 1}},
    {'command': ['http-mcp'], 'environment': {'TOKEN': 'private-value'}, 'secrets': {'TOKEN': ARN}},
])
def test_launcher_rejects_invalid_configuration_before_aws_or_exec(monkeypatch, capsys, config):
    module = launcher()
    if config is None:
        monkeypatch.delenv('DAERI_CONNECTOR_CONFIG', raising=False)
    else:
        monkeypatch.setenv('DAERI_CONNECTOR_CONFIG', config if isinstance(config, str) else json.dumps(config))
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: pytest.fail('Bad configuration initialized AWS'))
    monkeypatch.setattr(module.os, 'execvpe', lambda *args: pytest.fail('Bad configuration executed a process'))
    assert module.main([]) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)['error']['code'] == 'INVALID_INPUT'
    assert not output.out
    assert 'private' not in output.err and ARN not in output.err


@pytest.mark.parametrize('arguments', [['--command', 'other'], ['other'], ['--help']])
def test_launcher_rejects_command_line_override_without_echoing_arguments(monkeypatch, capsys, arguments):
    module = launcher()
    monkeypatch.setenv('DAERI_CONNECTOR_CONFIG', '{"command":["http-mcp"]}')
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: pytest.fail('Override initialized AWS'))
    monkeypatch.setattr(module.os, 'execvpe', lambda *args: pytest.fail('Override executed a process'))
    assert module.main(arguments) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)['error']['code'] == 'INVALID_INPUT'
    assert 'other' not in output.err


@pytest.mark.parametrize('response', [{}, {'SecretString': None}, {'SecretString': 123},
                                      {'SecretString': 'private\x00value'}, {'SecretBinary': b'private-value'}])
def test_launcher_rejects_unusable_secrets_without_leaking(monkeypatch, capsys, response):
    module = launcher()
    monkeypatch.setenv('DAERI_CONNECTOR_CONFIG', json.dumps({'command': ['http-mcp'], 'secrets': {'TOKEN': ARN}}))
    class Secrets:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_secret_value(self, **kwargs):
            return response
    monkeypatch.setattr(boto3, 'client', lambda *args, **kw: Secrets())
    monkeypatch.setattr(module.os, 'execvpe', lambda *args: pytest.fail('Unusable secret executed a process'))
    assert module.main([]) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)['error']['code'] == 'CONNECTION_NOT_READY'
    assert 'private' not in output.err and ARN not in output.err
    assert not output.out


@pytest.mark.parametrize('stage', ['client', 'secret', 'exec'])
def test_launcher_sanitizes_dependency_failures(monkeypatch, capsys, stage):
    module = launcher()
    monkeypatch.setenv('DAERI_CONNECTOR_CONFIG', json.dumps({'command': ['http-mcp'], 'secrets': {'TOKEN': ARN}}))
    class Secrets:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get_secret_value(self, **kwargs):
            if stage == 'secret':
                raise RuntimeError('private-token ' + ARN)
            return {'SecretString': 'private-token'}
    def client(*args, **kwargs):
        if stage == 'client':
            raise RuntimeError('private-token ' + ARN)
        return Secrets()
    def execute(*args):
        raise OSError('private-token ' + ARN)
    monkeypatch.setattr(boto3, 'client', client)
    monkeypatch.setattr(module.os, 'execvpe', execute)
    assert module.main([]) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)['error']['code'] == 'CONNECTION_NOT_READY'
    assert 'private' not in output.err and ARN not in output.err
    assert not output.out
