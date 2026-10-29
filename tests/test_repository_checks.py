import io
import json
from pathlib import Path
import shutil
import tarfile
import tomllib
import zipfile

import pytest

from scripts import check


ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()
    for name in ('pyproject.toml', 'uv.lock', 'AGENTS.md', 'CLAUDE.md', 'README.md'):
        shutil.copy2(ROOT / name, root / name)
    for name in ('.claude-plugin', '.codex-plugin', '.agents', 'recipes'):
        shutil.copytree(ROOT / name, root / name)
    source = root / 'src/daeri'
    source.mkdir(parents=True)
    (source / '__init__.py').write_text('"""Synthetic package."""\n')
    (source / 'config').mkdir()
    shutil.copy2(ROOT / 'src/daeri/config/catalog.json', source / 'config/catalog.json')
    return root


def test_metadata_versions_and_recipes_match(repository):
    assert check.check_metadata(repository) == []
    manifest = repository / '.codex-plugin/plugin.json'
    value = json.loads(manifest.read_text())
    value['version'] = '0.0.0'
    manifest.write_text(json.dumps(value))
    assert any('.codex-plugin/plugin.json' in issue for issue in check.check_metadata(repository))


def test_host_resource_catalog_needs_no_installed_recipe(repository):
    catalog = repository / 'src/daeri/config/catalog.json'
    value = json.loads(catalog.read_text())
    value['source-local'] = {'adapter': 'local_source', 'transport': 'host'}
    catalog.write_text(json.dumps(value))
    assert check.check_metadata(repository) == []


def test_catalog_and_lock_version_drift_are_reported(repository):
    lock = repository / 'uv.lock'
    lock.write_text(lock.read_text().replace(f'name = "daeri-agent"\nversion = "{VERSION}"',
                                            'name = "daeri-agent"\nversion = "0.0.0"'))
    catalog = repository / 'src/daeri/config/catalog.json'
    value = json.loads(catalog.read_text())
    value['db-postgres']['version'] = '0.0.0'
    catalog.write_text(json.dumps(value))
    issues = check.check_metadata(repository)
    assert any('uv.lock' in issue for issue in issues)
    assert any('db-postgres' in issue for issue in issues)


def write_archives(root, destination, *, omit=None, extra=None):
    destination.mkdir()
    with tarfile.open(destination / f'daeri_agent-{VERSION}.tar.gz', 'w:gz') as archive:
        for path in root.rglob('*'):
            if path.is_file() and path.relative_to(root).as_posix() != omit:
                archive.add(path, arcname=f'daeri_agent-{VERSION}/' + path.relative_to(root).as_posix())
        if extra:
            info = tarfile.TarInfo(f'daeri_agent-{VERSION}/' + extra)
            info.size = 4
            archive.addfile(info, io.BytesIO(b'test'))
    with zipfile.ZipFile(destination / f'daeri_agent-{VERSION}-py3-none-any.whl', 'w') as archive:
        for path in (root / 'src').rglob('*'):
            if path.is_file():
                archive.write(path, path.relative_to(root / 'src').as_posix())
        for path in (root / 'recipes').rglob('*'):
            if path.is_file():
                archive.write(path, 'daeri/' + path.relative_to(root).as_posix())


def test_archives_include_source_and_runtime_resources(repository, tmp_path):
    destination = tmp_path / 'dist'
    write_archives(repository, destination)
    assert check.check_archives(repository, destination) == []


@pytest.mark.parametrize('bad_file', ['.mcp.json', '.venv/bin/python', '../outside', 'build/report.json', 'docs/.DS_Store'])
def test_archives_reject_generated_or_escaping_files(repository, tmp_path, bad_file):
    destination = tmp_path / 'dist'
    write_archives(repository, destination, extra=bad_file)
    assert any(bad_file in issue for issue in check.check_archives(repository, destination))


def test_archives_report_missing_lock_and_stale_build(repository, tmp_path):
    destination = tmp_path / 'dist'
    write_archives(repository, destination, omit='uv.lock')
    assert any('uv.lock' in issue for issue in check.check_archives(repository, destination))
    shutil.copy2(next(destination.glob('*.whl')), destination / 'stale.whl')
    assert check.check_archives(repository, destination)


def test_archives_detect_outdated_source_bytes(repository, tmp_path):
    destination = tmp_path / 'dist'
    write_archives(repository, destination)
    (repository / 'src/daeri/__init__.py').write_text('changed after packaging\n')
    assert any('source bytes differ' in issue for issue in check.check_archives(repository, destination))


def test_archives_ignore_workstation_metadata_in_source(repository, tmp_path):
    (repository / 'docs').mkdir()
    (repository / 'docs/.DS_Store').write_bytes(b'workstation metadata')
    destination = tmp_path / 'dist'
    write_archives(repository, destination, omit='docs/.DS_Store')
    assert check.check_archives(repository, destination) == []


@pytest.mark.parametrize('error', [RuntimeError, AttributeError, TypeError])
def test_failed_check_keeps_logs_and_later_results(tmp_path, error):
    def failure(log):
        print('expected failure details', file=log)
        raise error('test failure')
    def success(log):
        print('later check completed', file=log)
    result = check.run_checks([('failure', failure), ('success', success)], tmp_path)
    assert result['status'] == 'failed'
    assert [step['status'] for step in result['steps']] == ['failed', 'passed']
    assert 'expected failure details' in (tmp_path / 'failure.log').read_text()
    assert json.loads((tmp_path / 'summary.json').read_text()) == result
    assert 'failure' in (tmp_path / 'summary.md').read_text()


def test_new_run_removes_previous_generated_results_before_failure(tmp_path):
    for name in ('pytest.xml', 'memory-recall.json', 'summary.json'):
        (tmp_path / name).write_text('stale success')
    (tmp_path / 'dist').mkdir()
    (tmp_path / 'dist/daeri_agent-old.whl').write_bytes(b'stale release')
    def failure(log):
        raise RuntimeError('current failure')
    result = check.run_checks([('failure', failure)], tmp_path)
    assert result['status'] == 'failed'
    assert not (tmp_path / 'pytest.xml').exists()
    assert not (tmp_path / 'memory-recall.json').exists()
    assert not (tmp_path / 'dist').exists()


def test_check_output_preserves_an_unrelated_directory(tmp_path):
    (tmp_path / 'keep.txt').write_text('user data')
    with pytest.raises(ValueError):
        check.run_checks([], tmp_path)
    assert (tmp_path / 'keep.txt').read_text() == 'user data'


def test_validation_commands_isolate_user_data_and_check_every_lock(monkeypatch, tmp_path):
    monkeypatch.setenv('DAERI_DATA_DIR', '/private/user/data')
    monkeypatch.setenv('DAERI_TEST_MODEL_CACHE', '/private/user/model')
    env = check.check_environment()
    assert 'DAERI_DATA_DIR' not in env and 'DAERI_TEST_MODEL_CACHE' not in env
    commands = check.lock_commands(ROOT, 'uv')
    assert len(commands) == 6
    assert all('--check' in command for command in commands)
    assert all('--no-python-downloads' in command for command in commands)


def test_previous_uv_generated_ignore_allows_repeated_check_output(tmp_path):
    distribution = tmp_path / 'dist'
    distribution.mkdir()
    (distribution / '.gitignore').write_bytes(b'*')
    (distribution / 'daeri_agent-old.whl').write_bytes(b'old generated package')
    result = check.run_checks([], tmp_path)
    assert result['status'] == 'passed'
    assert not distribution.exists()


def test_custom_ignore_in_previous_output_is_preserved(tmp_path):
    distribution = tmp_path / 'dist'
    distribution.mkdir()
    (distribution / '.gitignore').write_text('my-private-files')
    with pytest.raises(ValueError):
        check.run_checks([], tmp_path)
    assert (distribution / '.gitignore').read_text() == 'my-private-files'
