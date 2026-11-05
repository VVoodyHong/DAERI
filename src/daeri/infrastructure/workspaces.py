"""Task-isolated Git worktrees inside the configured project execution filesystem."""

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tempfile
from uuid import UUID

from daeri.core.errors import StoreError


def _run(arguments, **options):
    try:
        return subprocess.run(arguments, capture_output=True, text=True, **options)
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise StoreError('SOURCE_OPERATION_FAILED', 'The source operation could not finish; inspect its current state before retrying.') from None


def _git(path, *arguments, check=True):
    result = _run(['git', '-C', str(path), *arguments], timeout=180)
    if check and result.returncode:
        raise StoreError('SOURCE_OPERATION_FAILED', 'The Git operation failed; inspect the current repository before retrying.')
    return result


class GitWorkspaces:
    def __init__(self, root, repositories):
        if (not isinstance(repositories, dict) or not repositories
                or any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)
                       or not isinstance(source, str) or not source.strip() or source.startswith('-')
                       for name, source in repositories.items())):
            raise StoreError('INVALID_INPUT', 'Supply the project repository names and Git sources.')
        self.root, self.repositories = Path(root).resolve(), dict(repositories)

    def _paths(self, task_id, repository):
        try:
            task = str(UUID(task_id))
            source = self.repositories[repository]
        except (ValueError, TypeError, KeyError, AttributeError):
            raise StoreError('INVALID_INPUT', 'Supply a task UUID and a configured project repository.') from None
        directory = self.root / 'tasks' / task
        return source, self.root / 'repositories' / sha256(source.encode()).hexdigest(), directory / repository, directory / (repository + '.json')

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / '.workspace.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self, task_id, repository):
        source, cache, path, record = self._paths(task_id, repository)
        try:
            saved = json.loads(record.read_text())
            if (not isinstance(saved, dict) or saved.get('source') != source or saved.get('path') != str(path)
                    or _git(path, 'branch', '--show-current').stdout.strip() != saved['branch']
                    or _git(path, 'rev-parse', '--show-toplevel').stdout.strip() != str(path)):
                raise ValueError
            return saved, cache, path
        except (OSError, ValueError, KeyError):
            raise StoreError('SOURCE_WORKSPACE_UNAVAILABLE', 'The saved task workspace is unavailable or changed.') from None

    def prepare(self, task_id, repository, base_ref='HEAD'):
        source, cache, path, record = self._paths(task_id, repository)
        if not isinstance(base_ref, str) or not base_ref or base_ref.startswith('-') or any(char.isspace() for char in base_ref):
            raise StoreError('INVALID_INPUT', 'Supply a Git base reference.')
        with self._lock():
            if record.exists():
                saved, _, _ = self._read(task_id, repository)
                return {key: saved[key] for key in ('path', 'branch', 'base_commit')}
            if path.exists():
                raise StoreError('SOURCE_WORKSPACE_UNAVAILABLE', 'The task path already contains unregistered files.')
            cache.parent.mkdir(parents=True, exist_ok=True)
            if not cache.exists():
                with tempfile.TemporaryDirectory(prefix='clone-', dir=cache.parent) as directory:
                    cloned = Path(directory) / 'repository'
                    result = _run(['git', 'clone', '--bare', '--', source, str(cloned)], timeout=180)
                    if result.returncode:
                        raise StoreError('SOURCE_OPERATION_FAILED', 'The configured project repository could not be cloned.')
                    cloned.rename(cache)
            _git(cache, 'fetch', '--no-tags', 'origin', base_ref)
            base = _git(cache, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}').stdout.strip()
            branch = 'daeri/' + str(UUID(task_id)) + '/' + repository
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = _git(cache, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch, check=False).returncode == 0
            if _git(cache, 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch).stdout.strip():
                remote = 'refs/remotes/origin/' + branch
                _git(cache, 'fetch', '--no-tags', 'origin', '+refs/heads/' + branch + ':' + remote)
                if not existing:
                    _git(cache, 'branch', branch, remote)
                elif _git(cache, 'rev-parse', branch).stdout != _git(cache, 'rev-parse', remote).stdout:
                    if _git(cache, 'merge-base', '--is-ancestor', branch, remote, check=False).returncode == 0:
                        _git(cache, 'branch', '-f', branch, remote)
                    elif _git(cache, 'merge-base', '--is-ancestor', remote, branch, check=False).returncode != 0:
                        raise StoreError('SOURCE_WORKSPACE_CONFLICT',
                                         'The local and remote task branches diverged; reconcile their commits before resuming.')
                existing = True
            if existing:
                base = _git(cache, 'merge-base', base, branch).stdout.strip()
                _git(cache, 'worktree', 'add', str(path), branch)
            else:
                _git(cache, 'worktree', 'add', '-b', branch, str(path), base)
            saved = {'source': source, 'path': str(path), 'branch': branch, 'base_commit': base}
            record.write_text(json.dumps(saved), encoding='utf-8')
            return {key: saved[key] for key in ('path', 'branch', 'base_commit')}

    def status(self, task_id, repository):
        saved, _, path = self._read(task_id, repository)
        changes = _git(path, 'status', '--porcelain').stdout.splitlines()
        return {'path': str(path), 'branch': saved['branch'], 'base_commit': saved['base_commit'],
                'head_commit': _git(path, 'rev-parse', 'HEAD').stdout.strip(),
                'dirty': bool(changes), 'changes': changes}

    def verify(self, task_id, repository, target_ref='HEAD', command=None):
        saved, cache, path = self._read(task_id, repository)
        if _git(path, 'status', '--porcelain').stdout:
            raise StoreError('SOURCE_WORKSPACE_DIRTY', 'Commit the task changes before checking integration.')
        if (not isinstance(target_ref, str) or not target_ref or target_ref.startswith('-')
                or any(char.isspace() for char in target_ref)
                or (command is not None and (not isinstance(command, list) or not command
                    or any(not isinstance(item, str) or '\x00' in item for item in command)))):
            raise StoreError('INVALID_INPUT', 'Supply a target Git reference and optional test argument list.')
        with self._lock():
            _git(cache, 'fetch', '--no-tags', 'origin', target_ref)
            target = _git(cache, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}').stdout.strip()
            task_commit = _git(path, 'rev-parse', 'HEAD').stdout.strip()
            with tempfile.TemporaryDirectory(prefix='verify-', dir=self.root) as directory:
                integration = Path(directory) / 'repository'
                _git(cache, 'worktree', 'add', '--detach', str(integration), target)
                try:
                    merge = _git(integration, '-c', 'user.name=DAERI', '-c', 'user.email=daeri@example.invalid',
                                 'merge', '--no-commit', '--no-ff', task_commit, check=False)
                    conflicts = _git(integration, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
                    if merge.returncode and not conflicts:
                        raise StoreError('SOURCE_OPERATION_FAILED', 'The integration check could not complete.')
                    result = {'mergeable': not conflicts, 'conflicts': conflicts,
                              'target_commit': target, 'task_commit': task_commit}
                    if not conflicts and command:
                        test = _run(command, cwd=integration, timeout=300)
                        result['test'] = {'returncode': test.returncode, 'stdout': test.stdout[-8192:],
                                          'stderr': test.stderr[-8192:],
                                          'truncated': len(test.stdout) > 8192 or len(test.stderr) > 8192}
                    return result
                finally:
                    _git(cache, 'worktree', 'remove', '--force', str(integration))
