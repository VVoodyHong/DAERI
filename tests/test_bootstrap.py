"""Exercise preparation in a fake home without downloading or installing host tools."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installation(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX bootstrap behavior; Windows has separate PowerShell tests")
    root = tmp_path / "plugin with spaces"
    shutil.copytree(ROOT / "scripts", root / "scripts")
    home = tmp_path / "home with spaces"
    home.mkdir()
    commands = tmp_path / "bin"
    commands.mkdir()
    executable(commands / 'curl', "raise SystemExit('Unexpected download in this test')\n")
    env = dict(os.environ, HOME=str(home), PATH=f"{commands}:/usr/bin:/bin")
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    return root, home, commands, env


def executable(path, content):
    path.write_text(f"#!{sys.executable}\n" + content)
    path.chmod(0o755)


def fake_uv(path, *, fail=0, local_embedding=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    executable(path,
        "import json, os, pathlib, sys\n"
        "if sys.argv[1:] == ['--version']:\n print('uv 0.11.28');sys.exit(0)\n"
        f"if {fail}: sys.exit({fail})\n"
        "private = pathlib.Path(os.environ.get('DAERI_DATA_DIR', str(pathlib.Path(os.environ['HOME']) / 'Library/Application Support/DAERI'))).expanduser() / 'runtime'\n"
        "assert os.environ['UV_PYTHON_INSTALL_DIR'] == str(private / 'python')\n"
        "assert os.environ['UV_CACHE_DIR'] == str(private / 'cache')\n"
        "assert os.environ['UV_PYTHON_INSTALL_BIN'] == '0'\n"
        "assert os.environ['UV_PYTHON_INSTALL_REGISTRY'] == '0'\n"
        "assert 'UV_PYTHON_PREFERENCE' not in os.environ  # conflicts with --managed-python\n"
        "assert not any(key.startswith(('PYTHON', '_PYTHON', 'CONDA_')) or key in ['VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', '__PYVENV_LAUNCHER__', '_OLD_VIRTUAL_PATH', '_OLD_VIRTUAL_PYTHONHOME', 'PYLAUNCHER_DEBUG', 'UV_NO_PROJECT', 'UV_SYSTEM_PYTHON', 'UV_ISOLATED'] for key in os.environ)\n"
        "if sys.argv[1] == 'venv':\n assert sys.argv[2:-1] == ['--managed-python', '--no-config', '--relocatable', '--project', os.getcwd()]\n pathlib.Path(sys.argv[-1]).mkdir(exist_ok=True)\n (pathlib.Path(sys.argv[-1]) / 'relocatable-test-marker').touch()\n sys.exit(0)\n"
        "assert (pathlib.Path(os.environ['UV_PROJECT_ENVIRONMENT']) / 'relocatable-test-marker').exists()\n"
        f"assert sys.argv[1:] == ['sync', '--managed-python', '--no-config', '--frozen', '--no-dev', '--no-editable', '--project', os.getcwd()] + {['--extra', 'local-embedding'] if local_embedding else []!r}\n"
        "target = pathlib.Path(os.environ['UV_PROJECT_ENVIRONMENT']) / 'bin/python'\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        f"target.write_text('#!{sys.executable}\\nimport json, sys\\nif sys.argv[1:3]==[\"-I\",\"-c\"]: sys.exit(0)\\nassert sys.argv[1:]==[\"-I\",\"-m\",\"daeri.cli\",\"check\"]\\nprint(json.dumps(dict(ready=True)))\\n')\n"
        "target.chmod(0o755)\n")


def prepare(installation, *args):
    root, _, _, env = installation
    return subprocess.run(["/bin/sh", str(root / "scripts/setup.sh"), *args],
                          cwd=root.parent, env=env, capture_output=True, text=True, timeout=20)


def test_base_dependencies_exclude_local_model_runtime():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']
    assert not any(item.startswith(('fastembed', 'onnxruntime')) for item in project['dependencies'])
    assert 'fastembed>=0.9,<1' in project['optional-dependencies']['local-embedding']
    assert 'httpx>=0.28,<1' in project['dependencies']


def test_local_embedding_preparation_is_explicit_and_stays_in_private_environment(installation):
    root, _, _, _ = installation
    fake_uv(root / '.runtime/uv.exe', local_embedding=True)
    result = prepare(installation, '--prepare-local-embedding')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ready'] is True
    assert (root / '.venv/bin/python.exe').is_file()
    assert not list(root.glob('.venv.prepare.*'))
    assert not (root / '.daeri-setup.lock').exists()


@pytest.mark.parametrize('custom_data', [False, True])
def test_prepare_uses_private_uv_and_does_not_touch_user_shell_or_global_uv(installation, custom_data):
    root, home, commands, env = installation
    fake_uv(root / '.runtime/uv.exe')
    fake_uv(commands / "uv", fail=41)
    profiles = [home / name for name in ['.profile', '.zprofile', '.bash_profile']]
    for profile in profiles:
        profile.write_text('keep existing shell configuration\n')
    env['UV_PROJECT_ENVIRONMENT'] = str(home / 'unrelated environment')
    env.update(UV_NO_PROJECT='true', UV_SYSTEM_PYTHON='true', UV_ISOLATED='true',
               PYTHONPATH=str(home / 'unrelated packages'), PYTHONHOME=str(home / 'unrelated Python'),
               VIRTUAL_ENV=str(home / 'unrelated venv'), CONDA_PREFIX=str(home / 'unrelated conda'),
               __PYVENV_LAUNCHER__=str(home / 'unrelated launcher'), CONDA_DEFAULT_ENV='foreign',
               VIRTUAL_ENV_PROMPT='foreign', _PYTHON_SYSCONFIGDATA_PATH='foreign',
               _OLD_VIRTUAL_PATH='foreign', _OLD_VIRTUAL_PYTHONHOME='foreign', PYLAUNCHER_DEBUG='1')
    if custom_data:
        env['DAERI_DATA_DIR'] = str(home / 'custom DAERI data')
    result = prepare(installation, '--prepare')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ready'] is True
    assert (root / '.venv/bin/python.exe').is_file()
    assert (root / '.venv/bin/python.exe').is_symlink()
    assert all(profile.read_text() == 'keep existing shell configuration\n' for profile in profiles)
    assert not (home / 'unrelated environment').exists()
    assert not list(root.glob('.venv.prepare.*'))
    assert not (root / '.daeri-setup.lock').exists()


def test_failed_prepare_preserves_old_environment_and_profile_data(installation):
    root, home, commands, _ = installation
    fake_uv(root / '.runtime/uv.exe', fail=29)
    (root / '.venv').mkdir()
    (root / '.venv/previous-good').write_text('keep')
    (home / 'profile.json').write_text('keep profile')
    result = prepare(installation, '--prepare')
    assert result.returncode == 29
    assert (root / '.venv/previous-good').read_text() == 'keep'
    assert (home / 'profile.json').read_text() == 'keep profile'
    assert not list(root.glob('.venv.prepare.*'))
    assert not (root / '.daeri-setup.lock').exists()


@pytest.mark.parametrize('configured', ['relative data', './data', '../data', '~other-user/data'])
def test_prepare_rejects_relative_data_environment_before_changing_state(installation, configured):
    root, home, _, env = installation
    previous = root / '.venv.previous'
    previous.mkdir()
    (previous / 'keep').write_text('keep')
    env['DAERI_DATA_DIR'] = configured
    result = prepare(installation, '--prepare')
    assert result.returncode != 0
    assert 'DAERI_DATA_DIR' in result.stderr and 'absolute' in result.stderr
    assert (previous / 'keep').read_text() == 'keep'
    assert not (root / '.venv').exists()
    assert not (root / '.runtime').exists()
    assert not (root / '.daeri-setup.lock').exists()
    assert not (root.parent / configured / 'runtime').exists()
    assert list(home.iterdir()) == []


@pytest.mark.parametrize('configured', ['~', '~/custom DAERI data'])
def test_prepare_expands_current_user_home_in_data_environment(installation, configured):
    root, home, _, env = installation
    fake_uv(root / '.runtime/uv.exe')
    env['DAERI_DATA_DIR'] = configured
    result = prepare(installation, '--prepare')
    assert result.returncode == 0, result.stderr
    expected = home if configured == '~' else home / 'custom DAERI data'
    assert (expected / 'runtime/python').is_dir()
    assert (expected / 'runtime/cache').is_dir()
    assert not (root.parent / '~').exists()


def install_archive(installation, *, valid_hash=True):
    root, _, commands, _ = installation
    executable(commands / 'uname', "import sys\nprint('Darwin' if sys.argv[1:] == ['-s'] else 'arm64')\n")
    uv_source = commands / 'source-uv'
    fake_uv(uv_source)
    archive = commands / 'download.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        info = tarfile.TarInfo('uv-aarch64-apple-darwin/uv')
        payload = uv_source.read_bytes()
        info.size = len(payload)
        info.mode = 0o755
        output.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest() if valid_hash else '0' * 64
    manifest = root / 'scripts/dependencies.env'
    text = manifest.read_text()
    import re
    text = re.sub(r'^UV_DARWIN_ARM64_SHA256=.*$', 'UV_DARWIN_ARM64_SHA256=' + digest, text, flags=re.M)
    manifest.write_text(text)
    executable(commands / 'curl',
        "import pathlib, shutil, sys\n"
        f"assert sys.argv[-1] == 'https://releases.astral.sh/github/uv/releases/download/0.11.28/uv-aarch64-apple-darwin.tar.gz'\n"
        f"shutil.copyfile({str(archive)!r}, sys.argv[sys.argv.index('--output') + 1])\n")


def test_missing_private_uv_is_downloaded_without_changing_user_shell_path(installation):
    root, home, _, _ = installation
    install_archive(installation)
    result = prepare(installation, '--prepare')
    assert result.returncode == 0, result.stderr
    assert (root / '.runtime/uv.exe').is_file()
    assert not any((home / name).exists() for name in ['.profile', '.zprofile', '.bash_profile', '.local'])
    assert (root / '.venv/bin/python.exe').is_file()
    repeated = prepare(installation, '--prepare')
    assert repeated.returncode == 0, repeated.stderr
    assert not any((home / name).exists() for name in ['.profile', '.zprofile', '.bash_profile', '.local'])


def test_checksum_mismatch_never_executes_download(installation):
    root, home, _, _ = installation
    install_archive(installation, valid_hash=False)
    result = prepare(installation, '--prepare')
    assert result.returncode != 0
    assert 'checksum' in result.stderr.lower()
    assert not (root / '.runtime/uv.exe').exists()
    assert not (root / '.venv').exists()


def test_parallel_prepare_is_rejected_before_any_install(installation):
    root, _, _, _ = installation
    (root / '.daeri-setup.lock').mkdir()
    result = prepare(installation, '--prepare')
    assert result.returncode != 0
    assert 'already' in result.stderr.lower()
    assert not (root / '.venv').exists()


def test_plugin_configs_use_the_same_offline_os_independent_entrypoint():
    claude = json.loads((ROOT / '.mcp.json').read_text())['mcpServers']['daeri']
    codex = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())['mcpServers']['daeri']
    for server, variable in [(claude, '${CLAUDE_PLUGIN_ROOT}'), (codex, '${PLUGIN_ROOT}')]:
        assert server['command'] == variable + '/.venv/bin/python.exe'
        assert server['args'] == ['-I', '-m', 'daeri.server']
        assert server['env'] == {'__PYVENV_LAUNCHER__': ''}


def test_packaged_portable_mcp_entrypoint_initializes_from_an_unrelated_directory(tmp_path, plugin_environment):
    import asyncio
    from datetime import timedelta
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    plugin = plugin_environment
    environment = plugin / '.venv'
    portable = environment / 'bin/python.exe'
    subprocess.run([str(portable), '-I', '-c', 'import pathlib,sys; assert pathlib.Path(sys.prefix) == pathlib.Path(sys.argv[1])', str(environment)], check=True)
    config = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())['mcpServers']['daeri']
    args = [arg.replace('${PLUGIN_ROOT}', str(plugin)) for arg in config['args']]
    command = config['command'].replace('${PLUGIN_ROOT}', str(plugin))
    env = {'DAERI_DATA_DIR': str(tmp_path / 'data'), 'PYTHONHOME': str(tmp_path / 'foreign-python'),
           'PYTHONPATH': str(tmp_path / 'foreign-packages'), 'UV_NO_PROJECT': 'true',
           'UV_ISOLATED': 'true', 'UV_PROJECT_ENVIRONMENT': str(tmp_path / 'foreign-venv'),
           '__PYVENV_LAUNCHER__': str(tmp_path / 'foreign-launcher')}
    env.update(config['env'])

    async def scenario():
        parameters = StdioServerParameters(command=command, args=args, env=env, cwd=tmp_path)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=15)) as session:
                hello = await session.initialize()
                assert hello.serverInfo.name == 'daeri'
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {'daeri_task_create', 'daeri_plan_submit', 'daeri_approval_request'} <= names

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != 'nt', reason='Native PowerShell bootstrap behavior')
@pytest.mark.parametrize('configured', ['absolute', '~/custom DAERI data', '~\\custom DAERI data'])
@pytest.mark.parametrize('mode', ['-Prepare', '-PrepareLocalEmbedding'])
def test_windows_failed_prepare_keeps_previous_environment(tmp_path, configured, mode):
    root = tmp_path / 'plugin with spaces'
    shutil.copytree(ROOT / 'scripts', root / 'scripts')
    (root / '.venv').mkdir()
    (root / '.venv/previous-good').write_text('keep')
    private = root / '.runtime/uv.exe'
    private.parent.mkdir()
    source = 'public class FakeUv { public static int Main(string[] args) { if (args.Length == 1 && args[0] == "--version") { System.Console.WriteLine("uv 0.11.28"); return 0; } return 29; } }'
    compiler = 'Add-Type -TypeDefinition ' + "'" + source + "'" + ' -OutputType ConsoleApplication -OutputAssembly ' + "'" + str(private).replace("'", "''") + "'"
    subprocess.run(['powershell', '-NoProfile', '-Command', compiler], check=True, capture_output=True, text=True, timeout=30)
    setup_path = "'" + str(root / 'scripts/setup.ps1').replace("'", "''") + "'"
    wrapper = (
        "$Before = (Get-ChildItem Env: | Sort-Object Name | ForEach-Object { $_.Name + '=' + $_.Value }) -join \"`n\"; "
        "$UserBefore = [Environment]::GetEnvironmentVariable('Path', 'User'); "
        f"& {setup_path} {mode}; $Code = $LASTEXITCODE; "
        "$After = (Get-ChildItem Env: | Sort-Object Name | ForEach-Object { $_.Name + '=' + $_.Value }) -join \"`n\"; "
        "@{code=$Code; environment_unchanged=($Before -ceq $After); user_path_unchanged=($UserBefore -ceq [Environment]::GetEnvironmentVariable('Path', 'User'))} | ConvertTo-Json -Compress"
    )
    home = tmp_path / 'home'
    home.mkdir()
    data = str(tmp_path / 'data') if configured == 'absolute' else configured
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-Command', wrapper],
        env=dict(os.environ, DAERI_DATA_DIR=data, USERPROFILE=str(home), UV_NO_PROJECT='true',
                 PYTHONHOME='foreign', CONDA_PREFIX='foreign', VIRTUAL_ENV_PROMPT='foreign'),
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'code': 29, 'environment_unchanged': True, 'user_path_unchanged': True}
    assert (root / '.venv/previous-good').read_text() == 'keep'
    assert not list(root.glob('.venv.prepare.*'))
    assert not (root / '.daeri-setup.lock').exists()
    expected = tmp_path / 'data' if configured == 'absolute' else home / 'custom DAERI data'
    assert (expected / 'runtime/python').is_dir()
    assert (expected / 'runtime/cache').is_dir()


@pytest.mark.skipif(os.name != 'nt', reason='Native PowerShell bootstrap behavior')
@pytest.mark.parametrize('configured', ['relative-data', 'C:relative-data', '\\rooted-data', '~other-user/data'])
def test_windows_rejects_relative_data_environment_before_changing_state(tmp_path, configured):
    root = tmp_path / 'plugin with spaces'
    shutil.copytree(ROOT / 'scripts', root / 'scripts')
    previous = root / '.venv.previous'
    previous.mkdir()
    (previous / 'keep').write_text('keep')
    setup_path = "'" + str(root / 'scripts/setup.ps1').replace("'", "''") + "'"
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-Command',
         'function Invoke-WebRequest { throw "Unexpected download" }; & ' + setup_path + ' -Prepare; exit $LASTEXITCODE'],
        env=dict(os.environ, DAERI_DATA_DIR=configured), text=True, capture_output=True, timeout=30,
    )
    assert result.returncode != 0
    assert 'DAERI_DATA_DIR' in result.stderr and 'absolute' in result.stderr
    assert (previous / 'keep').read_text() == 'keep'
    assert not (root / '.venv').exists()
    assert not (root / '.runtime').exists()
    assert not (root / '.daeri-setup.lock').exists()


def test_runtime_check_does_not_require_global_uv(installation):
    root, _, _, _ = installation
    python = root / '.venv/bin/python.exe'
    python.parent.mkdir(parents=True)
    executable(python, "print('{\"ready\":true}')\n")
    result = prepare(installation, '--check')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ready'] is True


def test_base_server_and_environment_check_do_not_load_local_embedding_runtime(tmp_path):
    script = '''
import importlib.abc
import sys

class NoLocalEmbeddingRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastembed', 'onnxruntime'}:
            raise AssertionError('Base startup attempted to load a local embedding runtime')

sys.meta_path.insert(0, NoLocalEmbeddingRuntime())
import daeri.server
from daeri.cli import check_environment
assert check_environment()['ready'] is True
'''
    result = subprocess.run([sys.executable, '-I', '-c', script], cwd=tmp_path,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
