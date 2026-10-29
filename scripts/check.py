"""Run repository checks from the local development environment."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from time import perf_counter
import tomllib
import zipfile

from scripts.check_docs import check_docs


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ('pyproject.toml', 'uv.lock', 'AGENTS.md', 'CLAUDE.md', 'README.md')
SOURCE_DIRS = ('src', 'recipes', 'scripts', 'skills', 'agents', 'docs',
               '.claude-plugin', '.codex-plugin', '.agents', 'evaluations', 'tests')


def _toml(path):
    return tomllib.loads(path.read_text(encoding='utf-8'))


def check_metadata(root):
    project = _toml(root / 'pyproject.toml')['project']
    version = project['version']
    issues = []
    lock = _toml(root / 'uv.lock')
    package = [item for item in lock['package'] if item['name'] == project['name']]
    if len(package) != 1 or package[0]['version'] != version:
        issues.append('uv.lock: project version differs from pyproject.toml')
    for name in ('.claude-plugin/plugin.json', '.codex-plugin/plugin.json'):
        value = json.loads((root / name).read_text(encoding='utf-8'))
        if value.get('name') != 'daeri' or value.get('version') != version:
            issues.append(f'{name}: plugin name/version differs from pyproject.toml')
    marketplace = json.loads((root / '.claude-plugin/marketplace.json').read_text(encoding='utf-8'))
    entries = [item for item in marketplace['plugins'] if item['name'] == 'daeri']
    if len(entries) != 1 or entries[0].get('version') != version:
        issues.append('.claude-plugin/marketplace.json: plugin version differs from pyproject.toml')
    catalog = json.loads((root / 'src/daeri/config/catalog.json').read_text(encoding='utf-8'))
    for name, item in catalog.items():
        if item.get('transport') == 'host':
            continue
        recipe = _toml(root / 'recipes' / name / 'pyproject.toml')['project']
        if recipe['dependencies'] != [item['package'] + '==' + item['version']]:
            issues.append(f'recipes/{name}: direct dependency differs from catalog')
        if not (root / 'recipes' / name / 'uv.lock').is_file():
            issues.append(f'recipes/{name}/uv.lock: missing lock')
    return issues


def _forbidden(name):
    parts = PurePosixPath(name).parts
    return (name.startswith('/') or '\\' in name or '..' in parts
            or any(part in {'.git', '.runtime', '.pytest_cache', '__pycache__', 'build', 'dist',
                            '.daeri-setup.lock', '.mcp.json', '.DS_Store', 'Thumbs.db', 'Desktop.ini'}
                   or part.startswith(('.venv', '.mcp.prepare.')) for part in parts))


def _source_files(root):
    paths = [root / name for name in ROOT_FILES]
    for name in SOURCE_DIRS:
        paths.extend(path for path in (root / name).rglob('*')
                     if path.is_file() and not _forbidden(path.relative_to(root).as_posix()))
    return {path.relative_to(root).as_posix(): path for path in paths}


def check_archives(root, directory):
    version = _toml(root / 'pyproject.toml')['project']['version']
    wheels, sources = list(directory.glob('*.whl')), list(directory.glob('*.tar.gz'))
    if len(wheels) != 1 or len(sources) != 1:
        return ['build: expected exactly one current wheel and one sdist']
    if not wheels[0].name.startswith(f'daeri_agent-{version}-') or sources[0].name != f'daeri_agent-{version}.tar.gz':
        return ['build: distribution versions differ from pyproject.toml']
    files = _source_files(root)
    runtime = {name.removeprefix('src/'): path for name, path in files.items() if name.startswith('src/daeri/')}
    runtime.update({'daeri/' + name: path for name, path in files.items() if name.startswith('recipes/')})
    issues = []
    for distribution in (sources[0], wheels[0]):
        if distribution == sources[0]:
            with tarfile.open(distribution) as archive:
                members = [item for item in archive.getmembers() if not item.isdir()]
                names = [item.name for item in members]
                for member in members:
                    if not member.isfile():
                        issues.append(f'{distribution.name}: non-file entry: {member.name}')
                contents = {name.partition('/')[2]: archive.extractfile(member).read()
                            for name, member in zip(names, members) if member.isfile()}
            required = files
        else:
            with zipfile.ZipFile(distribution) as archive:
                names = [name for name in archive.namelist() if not name.endswith('/')]
                contents = {name: archive.read(name) for name in names}
            required = runtime
        if len(names) != len(set(names)):
            issues.append(f'{distribution.name}: duplicate entries')
        issues.extend(f'{distribution.name}: forbidden entry: {name}' for name in names if _forbidden(name))
        for name, path in required.items():
            if name not in contents:
                issues.append(f'{distribution.name}: missing {name}')
            elif contents[name] != path.read_bytes():
                issues.append(f'{distribution.name}: source bytes differ: {name}')
    return issues


def check_environment():
    env = dict(os.environ)
    # Bootstrap tests isolate HOME; these inherited overrides would escape that fixture.
    for name in ('DAERI_DATA_DIR', 'DAERI_TEST_MODEL_CACHE'):
        env.pop(name, None)
    return env


def lock_commands(root, uv):
    projects = [root, *sorted(path.parent for path in (root / 'recipes').glob('*/pyproject.toml'))]
    return [[uv, 'lock', '--check', '--no-python-downloads', '--project', str(project),
             '--python', sys.executable if project == root else '3.12'] for project in projects]


def _command(arguments, log, *, cwd=ROOT):
    print(shlex.join(map(str, arguments)), file=log, flush=True)
    subprocess.run(arguments, cwd=cwd, env=check_environment(), stdout=log,
                   stderr=subprocess.STDOUT, check=True, timeout=1200)


def _require_clean(issues, log):
    for issue in issues:
        print(issue, file=log)
    if issues:
        raise RuntimeError(f'{len(issues)} validation issue(s)')


def run_checks(steps, output):
    output.mkdir(parents=True, exist_ok=True)
    allowed = {'summary.json', 'summary.md', 'pytest.xml', 'memory-recall.json', 'dist',
               *(name + '.log' for name, _ in steps)}
    previous = list(output.iterdir())
    if any(path.name not in allowed or (path.is_dir() and path.name != 'dist') for path in previous):
        raise ValueError('Choose an empty directory or a previous check output directory.')
    for path in previous:
        if path.name == 'dist' and path.is_dir() and not path.is_symlink():
            for child in path.iterdir():
                generated_ignore = (child.name == '.gitignore' and child.is_file() and not child.is_symlink()
                                    and child.read_bytes() in (b'*', b'*\n'))
                if not generated_ignore and (not child.is_file() or not child.name.startswith('daeri_agent-')
                                             or not child.name.endswith(('.whl', '.tar.gz'))):
                    raise ValueError('The output dist directory contains unrelated files.')
    for path in previous:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    results = []
    for name, action in steps:
        started = perf_counter()
        status = 'passed'
        with (output / (name + '.log')).open('w', encoding='utf-8') as log:
            try:
                action(log)
            except Exception as error:
                # Malformed source metadata must also leave a result and allow independent checks.
                print(f'{type(error).__name__}: {error}', file=log)
                status = 'failed'
        results.append({'name': name, 'status': status, 'seconds': round(perf_counter() - started, 3),
                        'log': name + '.log'})
        print(f'{name}: {status}', flush=True)
        if status == 'failed':
            print((output / (name + '.log')).read_text(encoding='utf-8'), flush=True)
    report = {'status': 'passed' if all(step['status'] == 'passed' for step in results) else 'failed',
              'python': platform.python_version(), 'platform': platform.system(), 'steps': results,
              'boundaries': ['Actual models, private APIs, company resources and host UI are separate checks.',
                             'Pytest XML records conditional native/model skips.']}
    (output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    summary = ['# DAERI repository checks', '', f"Result: **{report['status']}**", '',
               '| Check | Result | Seconds |', '|---|---|---|']
    summary.extend(f"| {step['name']} | {step['status']} | {step['seconds']} |" for step in results)
    summary.extend(['', *report['boundaries'], ''])
    (output / 'summary.md').write_text('\n'.join(summary), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'build/checks')
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    uv = shutil.which('uv') or 'uv'

    def locks(log):
        for command in lock_commands(ROOT, uv):
            _command(command, log)

    def build(log):
        # A fresh directory keeps old release archives out of this verification.
        with TemporaryDirectory(prefix='daeri-build-') as directory:
            distribution = Path(directory)
            _command([uv, 'build', '--out-dir', str(distribution)], log)
            _require_clean(check_archives(ROOT, distribution), log)
            destination = output / 'dist'
            destination.mkdir(exist_ok=True)
            for path in distribution.iterdir():
                if path.name.endswith(('.whl', '.tar.gz')):
                    shutil.copy2(path, destination / path.name)

    steps = [
        ('metadata', lambda log: _require_clean(check_metadata(ROOT), log)),
        ('locks', locks),
        ('documentation', lambda log: _require_clean(check_docs(ROOT), log)),
        ('pytest', lambda log: _command([sys.executable, '-m', 'pytest', '-q',
            '--junitxml', str(output / 'pytest.xml')], log)),
        ('memory-recall', lambda log: _command([sys.executable, '-m', 'evaluations.memory_recall',
            '--output', str(output / 'memory-recall.json')], log)),
        ('build', build),
    ]
    try:
        report = run_checks(steps, output)
    except (OSError, ValueError) as error:
        print(f'Cannot prepare check output: {error}', file=sys.stderr)
        return 1
    print(f'Reports: {output}')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
