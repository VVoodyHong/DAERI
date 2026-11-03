"""Deployment commands retain safe error codes without initializing AWS for bad input."""

import importlib
import json
import sys

import boto3
import pytest
from yaml import safe_dump

from daeri.core.errors import StoreError
from test_delegation import CONFIG
from test_project_deployment import ARN, HOOK


@pytest.mark.parametrize('command', ['configure_subagents', 'configure_workspaces'])
@pytest.mark.parametrize('apply', [False, True])
@pytest.mark.parametrize('failure', ['duplicate_yaml', 'unknown_field', 'stored_error', 'unexpected_error'])
def test_deployment_commands_report_safe_error_codes_before_aws(tmp_path, monkeypatch, capsys, command, apply, failure):
    module = importlib.import_module('scripts.' + command)
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('Invalid input created AWS clients'))
    project = tmp_path / 'project.yaml'
    project.write_text(safe_dump({'project_id': 'payments', 'name': 'Payments',
                                 'harness_arn': ARN, 'approval_hook_arn': HOOK}))
    config = tmp_path / 'config.yaml'
    if failure == 'duplicate_yaml':
        config.write_text('private-key: private-value\nprivate-key: duplicate\n')
    else:
        valid = CONFIG if command == 'configure_subagents' else {
            'root': '/mnt/work/source', 'repositories': {'api': 'https://example.test/api.git'}}
        config.write_text(safe_dump({**valid, 'private-key': 'private-value'}))
    expected = 'INVALID_INPUT'
    if failure in {'stored_error', 'unexpected_error'}:
        injected_error = (StoreError('STORAGE_UNAVAILABLE', 'The configuration storage is unavailable.')
                          if failure == 'stored_error' else RuntimeError('private-key private-value'))
        def failed_read(path):
            raise injected_error
        monkeypatch.setattr(module, 'read_yaml', failed_read)
        expected = 'STORAGE_UNAVAILABLE' if failure == 'stored_error' else 'DEPLOYMENT_FAILED'
    arguments = [command, '--project-config', str(project), '--config', str(config)]
    if command == 'configure_workspaces':
        arguments += ['--skill-uri', 's3://workspace-skills/skills/']
    monkeypatch.setattr(sys, 'argv', arguments + (['--apply'] if apply else []))
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    reported = json.loads(output.err)['error']
    assert reported['code'] == expected
    if failure == 'stored_error':
        assert reported['message'] == 'The configuration storage is unavailable.'
    assert not output.out and 'private-key' not in output.err and 'private-value' not in output.err


@pytest.mark.parametrize('root', [None, 123, [], True, '/mnt/invalid\x00path'])
@pytest.mark.parametrize('apply', [False, True])
def test_workspace_command_reports_invalid_root_before_aws(tmp_path, monkeypatch, capsys, root, apply):
    from scripts.configure_workspaces import main

    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('Invalid input created AWS clients'))
    project = tmp_path / 'project.yaml'
    project.write_text(safe_dump({'project_id': 'payments', 'name': 'Payments',
                                 'harness_arn': ARN, 'approval_hook_arn': HOOK}))
    config = tmp_path / 'config.yaml'
    config.write_text(safe_dump({'root': root, 'repositories': {'api': 'https://example.test/api.git'}}))
    arguments = ['configure_workspaces', '--project-config', str(project), '--config', str(config),
                 '--skill-uri', 's3://workspace-skills/skills/']
    monkeypatch.setattr(sys, 'argv', arguments + (['--apply'] if apply else []))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().err)['error']['code'] == 'INVALID_INPUT'
