"""Management entrypoints classify normal first use before invoking private Python."""

import json
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SETUP_REQUIRED = {
    'status': 'setup_required',
    'message': 'DAERI 세팅이 필요합니다. setup을 실행해 주세요.',
}


@pytest.fixture
def management_plugin(tmp_path):
    root = tmp_path / 'plugin with spaces'
    shutil.copytree(ROOT / 'scripts', root / 'scripts')
    home = tmp_path / 'home with spaces'
    home.mkdir()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               DAERI_DATA_DIR=str(home / 'DAERI data'))
    return root, home, env


def invoke(plugin, *arguments):
    root, _, env = plugin
    if os.name == 'nt':
        powershell = shutil.which('powershell')
        if not powershell:
            pytest.skip('Native Windows PowerShell is unavailable')
        command = [powershell, '-NoProfile', '-ExecutionPolicy', 'RemoteSigned',
                   '-File', str(root / 'scripts/manage.ps1')]
    else:
        command = ['/bin/sh', str(root / 'scripts/manage.sh')]
    return subprocess.run(command + list(arguments), cwd=root.parent, env=env,
                          capture_output=True, text=True, timeout=20)


def prepare_private_python(plugin):
    root, _, _ = plugin
    environment = root / '.venv'
    subprocess.run([sys.executable, '-I', '-m', 'venv', '--without-pip', str(environment)],
                   check=True, capture_output=True, text=True, timeout=20)
    portable = environment / 'bin/python.exe'
    portable.parent.mkdir(exist_ok=True)
    if os.name == 'nt':
        shutil.copy2(environment / 'Scripts/python.exe', portable)
        packages = environment / 'Lib/site-packages'
    else:
        portable.symlink_to('python')
        packages = environment / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
    package = packages / 'daeri'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'cli.py').write_text(
        'import json, os, sys\n'
        "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'isolated': sys.flags.isolated, "
        "'foreign': {key: value for key, value in os.environ.items() "
        "if key.startswith(('UV_', 'PYTHON', '_PYTHON', 'CONDA_')) or key in "
        "['VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', '__PYVENV_LAUNCHER__', '_OLD_VIRTUAL_PATH', '_OLD_VIRTUAL_PYTHONHOME', 'PYLAUNCHER_DEBUG']}, "
        "'data_dir': os.environ['DAERI_DATA_DIR'], 'token': os.environ.get('RESOURCE_TEST_TOKEN')}))\n"
        "if os.environ.get('DAERI_TEST_STDERR'): print('resource call failed', file=sys.stderr)\n"
        "raise SystemExit(int(os.environ.get('DAERI_TEST_EXIT', '0')))\n"
    )
    return portable


@pytest.mark.parametrize('arguments', [('profile', 'list'), ('check',), ('setup', 'status', 'eu-mcp-prd')])
@pytest.mark.parametrize('partial_tools', [False, True])
def test_missing_runtime_is_a_normal_result_without_creating_user_data(management_plugin, arguments, partial_tools):
    root, home, _ = management_plugin
    if partial_tools:
        (root / '.runtime').mkdir()
    before = {str(path.relative_to(root)) for path in root.rglob('*')}
    result = invoke(management_plugin, *arguments)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == SETUP_REQUIRED
    assert result.stderr == ''
    assert {str(path.relative_to(root)) for path in root.rglob('*')} == before
    assert list(home.iterdir()) == []


@pytest.mark.parametrize('marker', ['.daeri-setup.lock', '.venv.previous', '.venv.prepare.interrupted'])
@pytest.mark.parametrize('ready', [False, True])
def test_interrupted_setup_requires_recovery_and_preserves_files(management_plugin, marker, ready):
    root, home, _ = management_plugin
    if ready:
        prepare_private_python(management_plugin)
    location = root / marker
    location.mkdir()
    (location / 'keep').write_text('preserve')
    result = invoke(management_plugin, 'profile', 'list')
    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert response['status'] == 'error'
    assert response['error']['code'] == 'RUNTIME_UNAVAILABLE'
    assert result.stderr == ''
    assert (location / 'keep').read_text() == 'preserve'
    assert list(home.iterdir()) == []


@pytest.mark.parametrize('damaged', ['empty_environment', 'directory_executable', 'broken_executable'])
def test_damaged_existing_environment_is_an_error(management_plugin, damaged):
    root, _, _ = management_plugin
    if damaged == 'empty_environment':
        (root / '.venv').mkdir()
    else:
        target = root / '.venv/bin/python.exe'
        target.parent.mkdir(parents=True)
        if damaged == 'directory_executable':
            target.mkdir()
        else:
            if os.name == 'nt':
                pytest.skip('POSIX broken interpreter symlink; Windows uses a copied executable')
            target.symlink_to('absent-python')
    result = invoke(management_plugin, 'profile', 'list')
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'RUNTIME_UNAVAILABLE'
    assert result.stderr == ''


@pytest.mark.skipif(os.name == 'nt', reason='POSIX runtime directory link and permission boundary')
@pytest.mark.parametrize('damaged', ['environment_link', 'bin_link', 'nonexecutable_python'])
def test_unsafe_runtime_paths_are_preserved_without_launch(management_plugin, damaged):
    root, _, _ = management_plugin
    external = root.parent / 'external'
    external.mkdir()
    if damaged == 'environment_link':
        (root / '.venv').symlink_to(external, target_is_directory=True)
    elif damaged == 'bin_link':
        (root / '.venv').mkdir()
        (root / '.venv/bin').symlink_to(external, target_is_directory=True)
    else:
        target = root / '.venv/bin/python.exe'
        target.parent.mkdir(parents=True)
        target.write_text('keep')
        target.chmod(0o600)
    result = invoke(management_plugin, 'profile', 'list')
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'RUNTIME_UNAVAILABLE'
    assert result.stderr == ''
    assert list(external.iterdir()) == []


@pytest.mark.parametrize('exit_code', [0, 23])
def test_management_forwards_arguments_output_and_exit_without_foreign_runtime_environment(management_plugin, exit_code):
    root, _, env = management_plugin
    prepare_private_python(management_plugin)
    env.update(UV_NO_PROJECT='true', PYTHONHOME='foreign', PYTHONPATH='foreign',
               VIRTUAL_ENV='foreign', VIRTUAL_ENV_PROMPT='foreign', CONDA_PREFIX='foreign',
               __PYVENV_LAUNCHER__='foreign', _OLD_VIRTUAL_PATH='foreign',
               _OLD_VIRTUAL_PYTHONHOME='foreign', PYLAUNCHER_DEBUG='1',
               RESOURCE_TEST_TOKEN='preserved resource credential', DAERI_TEST_EXIT=str(exit_code))
    if exit_code:
        env['DAERI_TEST_STDERR'] = 'yes'
    arguments = ['profile', 'import', 'a path with spaces.json', '', '{"name":"quoted value"}', 'ends with slash\\']
    result = invoke(management_plugin, *arguments)
    assert result.returncode == exit_code, result.stderr
    assert json.loads(result.stdout) == {
        'args': arguments, 'cwd': str(root.resolve()), 'isolated': 1, 'foreign': {},
        'data_dir': env['DAERI_DATA_DIR'], 'token': 'preserved resource credential',
    }
    assert result.stderr == ('resource call failed\n' if exit_code else '')


@pytest.fixture
def connection_plugin(plugin_environment, tmp_path):
    from daeri.infrastructure.storage import Store

    root = plugin_environment
    version = importlib.metadata.version('daeri-agent')
    (root / 'pyproject.toml').write_text('[project]\nversion = ' + json.dumps(version) + '\n')
    env = dict(os.environ, DAERI_DATA_DIR=str(tmp_path / 'data'))
    store = Store(tmp_path / 'data')
    try:
        profile = store.put_profile('shared-profile', embedding={'provider': 'keyword_only'})
    finally:
        store.close()
    packages = root / '.venv' / ('Lib/site-packages' if os.name == 'nt' else
                                f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    template = tmp_path / 'prepared environment'
    shutil.copytree(root / '.venv', template, symlinks=True)
    install = root / 'scripts/test_prepare.py'
    install.write_text(
        'import importlib.metadata, json, os, pathlib, re, shutil, sys, tomllib\n'
        f'root = pathlib.Path({str(root)!r})\n'
        f'packages = pathlib.Path({str(packages)!r})\n'
        f'template = pathlib.Path({str(template)!r})\n'
        "with (root / 'preparation.jsonl').open('a') as output: output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "with (root / 'preparation-data.jsonl').open('a') as output: output.write(json.dumps(os.environ.get('DAERI_DATA_DIR')) + '\\n')\n"
        "if not (root / '.venv').exists(): shutil.copytree(template, root / '.venv', symlinks=True)\n"
        "version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']\n"
        "metadata = importlib.metadata.distribution('daeri-agent').read_text('METADATA')\n"
        "target = packages / ('daeri_agent-' + version + '.dist-info')\n"
        "target.mkdir(exist_ok=True)\n"
        "(target / 'METADATA').write_text(re.sub(r'^Version: .+$', 'Version: ' + version, metadata, flags=re.M))\n"
        "if any('local' in item.lower() for item in sys.argv[1:]):\n"
        " (packages / 'fastembed.py').unlink(missing_ok=True)\n"
        " (packages / 'fastembed').mkdir(exist_ok=True)\n"
        " (packages / 'fastembed/__init__.py').write_text('')\n"
        "print('{\"ready\":true}')\n"
    )
    if os.name == 'nt':
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        (root / 'scripts/setup.ps1').write_text('& ' + quote(sys.executable) + ' ' + quote(install) + ' @args\nexit $LASTEXITCODE\n')
    else:
        import shlex
        (root / 'scripts/setup.sh').write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(install)) + ' "$@"\n')
    return (root, tmp_path, env), profile, packages


def test_connect_prepares_this_copy_and_reuses_the_existing_shared_profile(connection_plugin):
    plugin, profile, _ = connection_plugin
    root, _, env = plugin
    shutil.rmtree(root / '.venv')
    before = Path(env['DAERI_DATA_DIR']) / 'config'
    result = invoke(plugin, '--connect', 'profile', 'show', 'shared-profile')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['profile_id'] == profile['profile_id']
    assert (root / '.mcp.json').is_file()
    assert not before.exists()
    assert len((root / 'preparation.jsonl').read_text().splitlines()) == 1


def test_connect_reuses_unchanged_config_and_registered_store_when_environment_is_unset(connection_plugin):
    plugin, profile, _ = connection_plugin
    root, _, env = plugin
    first = invoke(plugin, '--connect', 'profile', 'show', 'shared-profile')
    assert first.returncode == 0, first.stdout + first.stderr
    target = root / '.mcp.json'
    identity = target.stat().st_ino, target.stat().st_mtime_ns
    env.pop('DAERI_DATA_DIR')
    second = invoke(plugin, '--connect', 'profile', 'show', 'shared-profile')
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)['profile_id'] == profile['profile_id']
    assert (target.stat().st_ino, target.stat().st_mtime_ns) == identity
    assert not (root / 'preparation.jsonl').exists()
    assert json.loads(second.stderr)['mcp_config_changed'] is False


def test_connect_honors_an_explicit_store_change_and_reports_reconnection(connection_plugin):
    from daeri.infrastructure.storage import Store

    plugin, _, _ = connection_plugin
    root, temporary, env = plugin
    assert invoke(plugin, '--connect', 'profile', 'list').returncode == 0
    other = temporary / 'other shared data'
    store = Store(other)
    try:
        profile = store.put_profile('other-profile', embedding={'provider': 'keyword_only'})
    finally:
        store.close()
    env['DAERI_DATA_DIR'] = str(other)
    result = invoke(plugin, '--connect', 'profile', 'show', 'other-profile')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['profile_id'] == profile['profile_id']
    report = json.loads(result.stderr)
    assert report['mcp_config_changed'] is True
    assert report['next_action'] == 'reload_host'
    assert json.loads((root / '.mcp.json').read_text())['mcpServers']['runtime']['env']['DAERI_DATA_DIR'] == str(other)


def test_connect_prepares_only_missing_local_packages_and_preserves_shared_models(connection_plugin):
    from daeri.infrastructure.storage import Store

    plugin, _, packages = connection_plugin
    root, temporary, env = plugin
    assert invoke(plugin, '--connect', 'profile', 'list').returncode == 0
    store = Store(Path(env['DAERI_DATA_DIR']))
    try:
        profile = store.put_profile('local-profile', embedding={'provider': 'local'})
    finally:
        store.close()
    model = Path(env['DAERI_DATA_DIR']) / 'models/prepared.json'
    model.parent.mkdir()
    model.write_text('existing model bytes')
    (packages / 'fastembed.py').write_text("raise ImportError('optional runtime is absent')\n")
    result = invoke(plugin, '--connect', 'profile', 'show', 'local-profile')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['profile_id'] == profile['profile_id']
    calls = [json.loads(line) for line in (root / 'preparation.jsonl').read_text().splitlines()]
    assert calls == [['-PrepareLocalEmbedding' if os.name == 'nt' else '--prepare-local-embedding']]
    assert model.read_text() == 'existing model bytes'
    report = json.loads(result.stderr.splitlines()[-1])
    assert report['mcp_config_changed'] is False
    assert report['next_action'] == 'reload_host'


@pytest.mark.parametrize('repair', ['missing', 'outdated'])
def test_connect_reuses_registered_store_before_preparing_a_repaired_environment(connection_plugin, repair):
    plugin, _, _ = connection_plugin
    root, temporary, env = plugin
    first = invoke(plugin, '--connect', 'profile', 'list')
    assert first.returncode == 0, first.stdout + first.stderr
    expected = env.pop('DAERI_DATA_DIR')
    if repair == 'missing':
        shutil.rmtree(root / '.venv')
        if os.name != 'nt' and sys.platform != 'darwin':
            native = root / 'scripts/test_plutil.py'
            native.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.load(open(sys.argv[-1]))["mcpServers"]["runtime"]["env"]["DAERI_DATA_DIR"])\n')
            native.chmod(0o700)
            script = root / 'scripts/manage.sh'
            script.write_text(script.read_text().replace('/usr/bin/plutil', '"' + str(native) + '"'))
    else:
        (root / 'pyproject.toml').write_text('[project]\nversion = "9.9.9"\n')
    result = invoke(plugin, '--connect', 'profile', 'list')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)[0]['name'] == 'shared-profile'
    assert json.loads((root / 'preparation-data.jsonl').read_text()) == expected
    report = json.loads(result.stderr.splitlines()[-1])
    assert report['mcp_config_changed'] is False
    assert report['next_action'] == 'reload_host'


@pytest.mark.parametrize('style', ['separate', 'inline', 'relative'])
def test_connect_uses_explicit_cli_storage_before_bootstrap(connection_plugin, style):
    from daeri.infrastructure.storage import Store

    plugin, _, _ = connection_plugin
    root, temporary, env = plugin
    assert invoke(plugin, '--connect', 'profile', 'list').returncode == 0
    explicit = temporary / 'explicit data'
    store = Store(explicit)
    try:
        profile = store.put_profile('explicit-profile', embedding={'provider': 'keyword_only'})
    finally:
        store.close()
    shutil.rmtree(root / '.venv')
    arguments = (['--data-dir=' + str(explicit)] if style == 'inline' else
                 ['--data-dir', '../explicit data' if style == 'relative' else str(explicit)])
    result = invoke(plugin, '--connect', *arguments, 'profile', 'show', 'explicit-profile')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['profile_id'] == profile['profile_id']
    assert Path(json.loads((root / 'preparation-data.jsonl').read_text())).resolve() == explicit
    assert json.loads((root / '.mcp.json').read_text())['mcpServers']['runtime']['env']['DAERI_DATA_DIR'] == str(explicit)
