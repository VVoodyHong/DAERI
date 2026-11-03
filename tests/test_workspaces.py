"""Real Git repositories exercise task isolation and integration checks."""

from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from daeri.core.errors import StoreError


def git(path, *arguments):
    return subprocess.run(['git', '-C', str(path), *arguments], check=True,
                          capture_output=True, text=True).stdout.strip()


def repository(path):
    path.mkdir()
    git(path, 'init', '-b', 'main')
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.invalid')
    (path / 'value.txt').write_text('original\n')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'Initial')
    return path


def test_multiple_tasks_and_repositories_have_isolated_branches_and_preserve_original(tmp_path):
    from daeri.infrastructure.workspaces import GitWorkspaces
    source = repository(tmp_path / 'source')
    second = repository(tmp_path / 'second')
    (source / 'private.txt').write_text('uncommitted')
    workspaces = GitWorkspaces(tmp_path / 'work', {'api': str(source), 'web': str(second)})
    first_id, other_id = str(uuid4()), str(uuid4())
    first = workspaces.prepare(first_id, 'api')
    other = workspaces.prepare(other_id, 'api')
    web = workspaces.prepare(first_id, 'web')
    assert len({item['path'] for item in (first, other, web)}) == 3
    assert first['branch'] != other['branch'] and first['base_commit'] == other['base_commit']
    (Path(first['path']) / 'value.txt').write_text('changed\n')
    assert (Path(other['path']) / 'value.txt').read_text() == 'original\n'
    assert (source / 'private.txt').read_text() == 'uncommitted'
    assert git(source, 'branch', '--show-current') == 'main'
    assert workspaces.prepare(first_id, 'api') == first
    assert workspaces.status(first_id, 'api')['dirty'] is True


def test_integration_check_detects_conflict_and_runs_tests_without_modifying_source(tmp_path):
    from daeri.infrastructure.workspaces import GitWorkspaces
    source = repository(tmp_path / 'source')
    workspaces = GitWorkspaces(tmp_path / 'work', {'api': str(source)})
    task_id = str(uuid4())
    task = workspaces.prepare(task_id, 'api')
    path = Path(task['path'])
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.invalid')
    (path / 'added.txt').write_text('verified\n')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'Task')
    result = workspaces.verify(task_id, 'api', command=['git', 'diff', '--check'])
    assert result['mergeable'] and result['test']['returncode'] == 0
    (path / 'value.txt').write_text('task change\n')
    git(path, 'commit', '-am', 'Task conflict')
    (source / 'value.txt').write_text('upstream change\n')
    git(source, 'commit', '-am', 'Upstream')
    head = git(source, 'rev-parse', 'HEAD')
    result = workspaces.verify(task_id, 'api')
    assert not result['mergeable'] and 'value.txt' in result['conflicts']
    assert git(source, 'rev-parse', 'HEAD') == head
    assert not git(source, 'status', '--porcelain')


@pytest.mark.parametrize('task,repo', [('bad/../../id', 'api'), (str(uuid4()), '../api'), (str(uuid4()), 'missing')])
def test_invalid_workspace_scope_is_rejected_without_git_execution(tmp_path, task, repo):
    from daeri.infrastructure.workspaces import GitWorkspaces
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        GitWorkspaces(tmp_path / 'work', {'api': '/unused'}).prepare(task, repo)


def test_simultaneous_tasks_share_the_cache_and_push_their_own_branches(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from daeri.infrastructure.workspaces import GitWorkspaces
    source = repository(tmp_path / 'source')
    workspaces = GitWorkspaces(tmp_path / 'work', {'api': str(source)})
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = list(pool.map(lambda task: workspaces.prepare(task, 'api'), [str(uuid4()), str(uuid4())]))
    assert tasks[0]['path'] != tasks[1]['path']
    for task in tasks:
        path = Path(task['path'])
        git(path, 'config', 'user.name', 'Test')
        git(path, 'config', 'user.email', 'test@example.invalid')
        (path / 'branch.txt').write_text(task['branch'])
        git(path, 'add', '.')
        git(path, 'commit', '-m', 'Task change')
        git(path, 'push', 'origin', 'HEAD:refs/heads/' + task['branch'])
        assert git(source, 'rev-parse', task['branch']) == git(path, 'rev-parse', 'HEAD')
    assert git(source, 'branch', '--show-current') == 'main'


def test_test_timeout_reports_failure_and_removes_only_its_disposable_worktree(tmp_path, monkeypatch):
    from daeri.infrastructure.workspaces import GitWorkspaces
    source = repository(tmp_path / 'source')
    workspaces = GitWorkspaces(tmp_path / 'work', {'api': str(source)})
    task_id = str(uuid4())
    task = workspaces.prepare(task_id, 'api')
    original_run = subprocess.run
    def timed_out(arguments, **options):
        if arguments == ['task-test']:
            raise subprocess.TimeoutExpired(arguments, 300)
        return original_run(arguments, **options)
    monkeypatch.setattr('daeri.infrastructure.workspaces.subprocess.run', timed_out)
    with pytest.raises(StoreError, match='SOURCE_OPERATION_FAILED'):
        workspaces.verify(task_id, 'api', command=['task-test'])
    assert Path(task['path']).exists()
    assert not list((tmp_path / 'work').glob('verify-*'))
    assert workspaces.status(task_id, 'api')['dirty'] is False


def test_a_new_execution_filesystem_resumes_the_pushed_task_branch(tmp_path):
    from daeri.infrastructure.workspaces import GitWorkspaces
    source = repository(tmp_path / 'source')
    first = GitWorkspaces(tmp_path / 'first-session', {'api': str(source)})
    task_id = str(uuid4())
    original = first.prepare(task_id, 'api')
    path = Path(original['path'])
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.invalid')
    (path / 'committed.txt').write_text('Preserved after a new session\n')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'Task checkpoint')
    git(path, 'push', 'origin', 'HEAD:refs/heads/' + original['branch'])
    (path / 'uncommitted.txt').write_text('Only the old filesystem has this')
    second = GitWorkspaces(tmp_path / 'new-session', {'api': str(source)})
    resumed = second.prepare(task_id, 'api')
    assert resumed['base_commit'] == original['base_commit']
    assert resumed['branch'] == original['branch']
    assert (Path(resumed['path']) / 'committed.txt').read_text() == 'Preserved after a new session\n'
    assert not (Path(resumed['path']) / 'uncommitted.txt').exists()
    assert git(Path(resumed['path']), 'rev-parse', 'HEAD') == git(path, 'rev-parse', 'HEAD')


def test_an_existing_cache_fetches_a_task_branch_pushed_after_it_was_cloned(tmp_path):
    from daeri.infrastructure.workspaces import GitWorkspaces
    source = repository(tmp_path / 'source')
    cached = GitWorkspaces(tmp_path / 'cached-session', {'api': str(source)})
    cached.prepare(str(uuid4()), 'api')
    other = GitWorkspaces(tmp_path / 'other-session', {'api': str(source)})
    task_id = str(uuid4())
    task = other.prepare(task_id, 'api')
    path = Path(task['path'])
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.invalid')
    (path / 'checkpoint.txt').write_text('Created after the first cache clone\n')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'Later checkpoint')
    git(path, 'push', 'origin', 'HEAD:refs/heads/' + task['branch'])
    resumed = cached.prepare(task_id, 'api')
    assert (Path(resumed['path']) / 'checkpoint.txt').read_text() == 'Created after the first cache clone\n'
    assert git(Path(resumed['path']), 'rev-parse', 'HEAD') == git(path, 'rev-parse', 'HEAD')


@pytest.mark.parametrize('history', ['remote_ahead', 'local_ahead', 'diverged'])
def test_imported_task_branch_reconciles_remote_updates_and_preserves_local_commits(tmp_path, history):
    from daeri.infrastructure.workspaces import GitWorkspaces
    source = repository(tmp_path / 'source')
    first = GitWorkspaces(tmp_path / 'first-session', {'api': str(source)})
    task_id = str(uuid4())
    task = first.prepare(task_id, 'api')
    path = Path(task['path'])
    git(path, 'config', 'user.name', 'Test')
    git(path, 'config', 'user.email', 'test@example.invalid')
    (path / 'first.txt').write_text('First checkpoint\n')
    git(path, 'add', '.')
    git(path, 'commit', '-m', 'First checkpoint')
    git(path, 'push', 'origin', 'HEAD:refs/heads/' + task['branch'])
    cached = GitWorkspaces(tmp_path / 'cached-session', {'api': str(source)})
    cached.prepare(str(uuid4()), 'api')
    cache = next((cached.root / 'repositories').iterdir())
    local_commit = git(cache, 'rev-parse', task['branch'])
    if history in {'local_ahead', 'diverged'}:
        local = tmp_path / 'local-only'
        git(cache, 'worktree', 'add', str(local), task['branch'])
        git(local, 'config', 'user.name', 'Test')
        git(local, 'config', 'user.email', 'test@example.invalid')
        (local / 'local.txt').write_text('Unpushed local commit\n')
        git(local, 'add', '.')
        git(local, 'commit', '-m', 'Local checkpoint')
        local_commit = git(local, 'rev-parse', 'HEAD')
        git(cache, 'worktree', 'remove', str(local))
    if history in {'remote_ahead', 'diverged'}:
        (path / 'remote.txt').write_text('Later remote checkpoint\n')
        git(path, 'add', '.')
        git(path, 'commit', '-m', 'Remote checkpoint')
        git(path, 'push', 'origin', 'HEAD:refs/heads/' + task['branch'])
    if history == 'diverged':
        with pytest.raises(StoreError, match='SOURCE_WORKSPACE_CONFLICT'):
            cached.prepare(task_id, 'api')
        assert git(cache, 'rev-parse', task['branch']) == local_commit
        assert not (cached.root / 'tasks' / task_id / 'api').exists()
    else:
        resumed = Path(cached.prepare(task_id, 'api')['path'])
        expected = git(path, 'rev-parse', 'HEAD') if history == 'remote_ahead' else local_commit
        assert git(resumed, 'rev-parse', 'HEAD') == expected
        assert (resumed / ('remote.txt' if history == 'remote_ahead' else 'local.txt')).exists()
