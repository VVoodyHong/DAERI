"""Management entrypoints classify normal first use before invoking private Python."""

import json
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
