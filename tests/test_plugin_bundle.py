"""A deployment bundle resolves public connection settings without installing hosts."""

import json
import importlib.util
from pathlib import Path
import subprocess
import shutil
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def build(output, *options):
    return subprocess.run([
        sys.executable, str(ROOT / 'scripts/build_plugin.py'),
        '--endpoint', 'https://daeri.example.com/mcp',
        '--client-id', 'company-public-client', '--callback-port', '59383',
        '--output', str(output), *options,
    ], capture_output=True, text=True, timeout=10)


def test_bundle_contains_both_host_manifests_resolved_connection_and_shared_skill(tmp_path):
    output = tmp_path / 'daeri.zip'
    result = build(output)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as bundle:
        names = set(bundle.namelist())
        package = 'plugins/daeri/'
        for host in ('.claude-plugin', '.codex-plugin'):
            manifest = json.loads(bundle.read(package + host + '/plugin.json'))
            assert manifest['name'] == 'daeri'
            assert package + manifest['mcpServers'].removeprefix('./') in names
            assert package + manifest['skills'].removeprefix('./').rstrip('/') + '/daeri/SKILL.md' in names
        connection = json.loads(bundle.read(package + '.mcp.json'))
        assert connection == {'mcpServers': {'runtime': {
            'type': 'http', 'url': 'https://daeri.example.com/mcp',
            'oauth': {'clientId': 'company-public-client', 'callbackPort': 59383},
        }}}
        for catalog_path in ('.claude-plugin/marketplace.json', '.agents/plugins/marketplace.json'):
            catalog = json.loads(bundle.read(catalog_path))
            assert catalog['name'] == 'daeri'
            plugin = catalog['plugins'][0]
            source = plugin['source']
            path = source['path'] if isinstance(source, dict) else source
            assert path == './plugins/daeri'
        assert all('..' not in Path(name).parts for name in names)
        assert not any(name.endswith(('.pyc', '.env')) for name in names)
        assert all(b'__DAERI_' not in bundle.read(name) for name in names)


@pytest.mark.parametrize('options', [
    ('--endpoint', 'http://daeri.example/mcp'),
    ('--endpoint', 'https://secret@daeri.example/mcp'),
    ('--endpoint', 'https://daeri.example/mcp#fragment'),
    ('--endpoint', 'https://daeri.example/mcp?token=do-not-package'),
    ('--endpoint', 'https://daeri.example/wrong-route'),
    ('--client-id', ''), ('--callback-port', '0'), ('--callback-port', '65536'),
])
def test_invalid_connection_settings_do_not_create_an_archive(tmp_path, options):
    output = tmp_path / 'daeri.zip'
    result = build(output, *options)
    assert result.returncode != 0
    assert not output.exists()


def test_existing_distribution_is_never_overwritten(tmp_path):
    output = tmp_path / 'daeri.zip'
    output.write_bytes(b'existing distribution')
    result = build(output)
    assert result.returncode != 0
    assert output.read_bytes() == b'existing distribution'


def test_build_requires_an_operator_to_supply_every_deployment_value(tmp_path):
    result = subprocess.run([
        sys.executable, str(ROOT / 'scripts/build_plugin.py'),
        '--output', str(tmp_path / 'daeri.zip'),
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert not (tmp_path / 'daeri.zip').exists()


def test_bundle_does_not_collect_unrelated_or_secret_files_from_the_source_tree(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    shutil.copytree(ROOT / 'plugins', source / 'plugins')
    (source / 'pyproject.toml').write_text('[project]\nversion="0.1.0"\n')
    (source / 'plugins/daeri/.env').write_text('TOKEN=not-for-distribution')
    outside = tmp_path / 'private.txt'
    outside.write_text('private file')
    (source / 'plugins/daeri/private.txt').symlink_to(outside)
    spec = importlib.util.spec_from_file_location('build_plugin_for_test', ROOT / 'scripts/build_plugin.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder, 'ROOT', source)
    output = tmp_path / 'daeri.zip'
    builder.build_plugin('https://daeri.example/mcp', 'public-client', 59383, output)
    with zipfile.ZipFile(output) as archive:
        assert 'plugins/daeri/.env' not in archive.namelist()
        assert 'plugins/daeri/private.txt' not in archive.namelist()


def test_runtime_public_url_is_preserved_in_both_host_distribution(tmp_path):
    endpoint = ('https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/'
                'arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123456789012%3Aruntime%2Fdaeri-1234567890'
                '/invocations?qualifier=PROD')
    output = tmp_path / 'runtime.zip'
    result = build(output, '--endpoint', endpoint)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(output) as bundle:
        connection = json.loads(bundle.read('plugins/daeri/.mcp.json'))
        assert connection['mcpServers']['runtime']['url'] == endpoint
