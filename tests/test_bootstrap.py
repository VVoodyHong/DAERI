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
    env = dict(os.environ, HOME=str(home), PATH=f"{commands}:/usr/bin:/bin")
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    return root, home, commands, env


def executable(path, content):
    path.write_text(f"#!{sys.executable}\n" + content)
    path.chmod(0o755)


def fake_uv(path, *, fail=0):
    executable(path,
        "import json, os, pathlib, sys\n"
        "if sys.argv[1:] == ['--version']:\n print('uv 0.11.28');sys.exit(0)\n"
        f"if {fail}: sys.exit({fail})\n"
        "if sys.argv[1] == 'venv':\n assert sys.argv[2:-1] == ['--relocatable', '--project', os.getcwd()]\n pathlib.Path(sys.argv[-1]).mkdir(exist_ok=True)\n (pathlib.Path(sys.argv[-1]) / 'relocatable-test-marker').touch()\n sys.exit(0)\n"
        "assert (pathlib.Path(os.environ['UV_PROJECT_ENVIRONMENT']) / 'relocatable-test-marker').exists()\n"
        "assert sys.argv[1:] == ['sync', '--frozen', '--no-dev', '--no-editable', '--project', os.getcwd()]\n"
        "target = pathlib.Path(os.environ['UV_PROJECT_ENVIRONMENT']) / 'bin/python'\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        f"target.write_text('#!{sys.executable}\\nimport json, sys\\nassert sys.argv[1:]==[\"-m\",\"daeri.cli\",\"check\"]\\nprint(json.dumps(dict(ready=True)))\\n')\n"
        "target.chmod(0o755)\n")


def prepare(installation, *args):
    root, _, _, env = installation
    return subprocess.run(["/bin/sh", str(root / "scripts/setup.sh"), *args],
                          env=env, capture_output=True, text=True, timeout=20)


def test_prepare_with_existing_uv_publishes_only_checked_environment(installation):
    root, home, commands, env = installation
    fake_uv(commands / "uv")
    env['UV_PROJECT_ENVIRONMENT'] = str(home / 'unrelated environment')
    result = prepare(installation, '--prepare')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ready'] is True
    assert (root / '.venv/bin/python').exists()
    assert not (home / 'unrelated environment').exists()
    assert not list(root.glob('.venv.prepare.*'))
    assert not (root / '.daeri-setup.lock').exists()


def test_failed_prepare_preserves_old_environment_and_profile_data(installation):
    root, home, commands, _ = installation
    fake_uv(commands / 'uv', fail=29)
    (root / '.venv').mkdir()
    (root / '.venv/previous-good').write_text('keep')
    (home / 'profile.json').write_text('keep profile')
    result = prepare(installation, '--prepare')
    assert result.returncode == 29
    assert (root / '.venv/previous-good').read_text() == 'keep'
    assert (home / 'profile.json').read_text() == 'keep profile'
    assert not list(root.glob('.venv.prepare.*'))
    assert not (root / '.daeri-setup.lock').exists()


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


def test_missing_uv_is_downloaded_verified_and_added_to_user_shell_path(installation):
    root, home, _, _ = installation
    install_archive(installation)
    result = prepare(installation, '--prepare')
    assert result.returncode == 0, result.stderr
    assert (home / '.local/share/daeri/tools/uv/0.11.28/uv').is_file()
    assert 'DAERI' in (home / '.zprofile').read_text()
    assert 'Restart' in result.stderr
    assert (root / '.venv/bin/python').is_file()
    repeated = prepare(installation, '--prepare')
    assert repeated.returncode == 0, repeated.stderr
    assert (home / '.zprofile').read_text().count('# DAERI') == 1


def test_checksum_mismatch_never_executes_download(installation):
    root, home, _, _ = installation
    install_archive(installation, valid_hash=False)
    result = prepare(installation, '--prepare')
    assert result.returncode != 0
    assert 'checksum' in result.stderr.lower()
    assert not (home / '.local/share/daeri/tools/uv/0.11.28/uv').exists()
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
        assert server['command'] == 'uv'
        assert server['env']['UV_PROJECT_ENVIRONMENT'] == variable + '/.venv'
        assert server['args'] == ['run', '--offline', '--no-sync', '--project', variable,
                                  'python', '-m', 'daeri.server']


def test_packaged_portable_mcp_entrypoint_initializes_from_an_unrelated_directory(tmp_path):
    import asyncio
    from datetime import timedelta
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    uv = shutil.which('uv')
    if uv is None:
        pytest.skip('uv is required for the packaged launcher integration')
    config = json.loads((ROOT / '.codex-plugin/plugin.json').read_text())['mcpServers']['daeri']
    args = [arg.replace('${PLUGIN_ROOT}', str(ROOT)) for arg in config['args']]
    env = {key: value.replace('${PLUGIN_ROOT}', str(ROOT)) for key, value in config['env'].items()}
    env['DAERI_DATA_DIR'] = str(tmp_path / 'data')

    async def scenario():
        parameters = StdioServerParameters(command=uv, args=args, env=env, cwd=tmp_path)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=15)) as session:
                hello = await session.initialize()
                assert hello.serverInfo.name == 'daeri'
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {'daeri_task_create', 'daeri_plan_submit', 'daeri_approval_request'} <= names

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != 'nt', reason='Native PowerShell bootstrap behavior')
def test_windows_failed_prepare_keeps_previous_environment(tmp_path):
    root = tmp_path / 'plugin with spaces'
    shutil.copytree(ROOT / 'scripts', root / 'scripts')
    (root / '.venv').mkdir()
    (root / '.venv/previous-good').write_text('keep')
    commands = tmp_path / 'commands'
    commands.mkdir()
    fake = commands / 'fake_uv.py'
    fake.write_text("import sys\nif sys.argv[1:]==['--version']: print('uv 0.11.28');sys.exit(0)\nsys.exit(29)\n")
    (commands / 'uv.cmd').write_text(f'@"{sys.executable}" "{fake}" %*\n')
    result = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned', '-File',
         str(root / 'scripts/setup.ps1'), '-Prepare'],
        env=dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH']),
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 29, result.stderr
    assert (root / '.venv/previous-good').read_text() == 'keep'
    assert not list(root.glob('.venv.prepare.*'))
    assert not (root / '.daeri-setup.lock').exists()


def test_runtime_check_does_not_claim_portable_launch_ready_without_uv(installation):
    root, _, _, _ = installation
    python = root / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    executable(python, "print('{\"ready\":true}')\n")
    result = prepare(installation, '--check')
    assert result.returncode != 0
    assert 'uv' in result.stderr
    assert result.stdout == ''
