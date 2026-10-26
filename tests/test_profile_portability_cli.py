import getpass
import io
import json
import os
from pathlib import Path
import sys

import pytest

import daeri.config.profile_portability as profile_portability
from daeri import cli
from daeri.core.errors import StoreError


PASSWORD = 'PRIVATE_ARCHIVE_PASSPHRASE'


@pytest.fixture
def invoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'stdin', io.StringIO())

    def run(*arguments, data_dir=None):
        monkeypatch.setattr(sys, 'argv', ['daeri', '--data-dir', str(data_dir or tmp_path / 'data'), 'profile', *arguments])
        code = cli.main()
        captured = capsys.readouterr()
        assert PASSWORD not in captured.out + captured.err
        return code, json.loads(captured.out), captured.err

    return run


def password_file(tmp_path, content=PASSWORD.encode(), mode=0o600):
    path = tmp_path / 'password'
    path.write_bytes(content)
    path.chmod(mode)
    return path


@pytest.mark.parametrize('arguments', [('backup', 'alpha', 'archive'), ('restore', 'archive')])
def test_noninteractive_archive_commands_require_private_input_without_opening_data(tmp_path, invoke, arguments):
    code, result, stderr = invoke(*arguments)
    assert code == 0 and result['status'] == 'input_required'
    assert not (tmp_path / 'data').exists()
    assert not (tmp_path / 'archive').exists()
    assert stderr == ''


@pytest.mark.parametrize('content', [PASSWORD.encode(), PASSWORD.encode() + b'\n', PASSWORD.encode() + b'\r\n',
                                    ('é' * 4).encode(), b'x' * 1024 + b'\n'])
def test_restore_preview_reads_one_private_utf8_password_line_without_opening_store(tmp_path, monkeypatch, invoke, content):
    path = password_file(tmp_path, content)
    received = []

    def inspect(archive, passphrase):
        received.append((archive, passphrase))
        return {'confirmation_token': 'token', 'name': 'alpha'}

    monkeypatch.setattr(profile_portability, 'inspect_backup', inspect)
    code, result, stderr = invoke('restore', str(tmp_path / 'archive'), '--password-file', str(path))
    assert code == 0 and result == {'confirmation_token': 'token', 'name': 'alpha'}
    assert received == [(tmp_path / 'archive', content.rstrip(b'\r\n').decode())]
    assert not (tmp_path / 'data').exists()
    assert path.read_bytes() == content and stderr == ''


@pytest.mark.parametrize('kind', ['short', 'oversized', 'two_lines', 'two_newlines', 'invalid_utf8',
                                 'symlink', 'directory', 'missing', 'public_mode'])
def test_archive_password_file_rejects_unsafe_input_before_opening_store(tmp_path, monkeypatch, invoke, kind):
    contents = {'short': b'short', 'oversized': b'x' * 1026, 'two_lines': b'password\npassword',
                'two_newlines': PASSWORD.encode() + b'\n\n', 'invalid_utf8': b'\xff' * 8}
    path = password_file(tmp_path, contents.get(kind, PASSWORD.encode()))
    if kind == 'symlink':
        linked = tmp_path / 'linked'
        linked.symlink_to(path)
        path = linked
    elif kind == 'directory':
        path = tmp_path
    elif kind == 'missing':
        path = tmp_path / 'missing'
    elif kind == 'public_mode':
        if os.name == 'nt':
            pytest.skip('POSIX private-file permission contract')
        path.chmod(0o644)

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid password inputs must be rejected before archive inspection.')

    monkeypatch.setattr(profile_portability, 'inspect_backup', forbidden)
    code, result, stderr = invoke('restore', 'archive', '--password-file', str(path))
    assert code == 1 and result['error']['code'] == 'INVALID_INPUT'
    assert not (tmp_path / 'data').exists() and stderr == ''


def test_backup_owns_one_runtime_and_closes_it_after_failure(tmp_path, monkeypatch, invoke):
    import daeri.application.runtime

    path = password_file(tmp_path)
    events = []

    class Connection:
        def close(self):
            events.append('closed')

    class Runtime:
        def __init__(self, data_dir):
            assert data_dir == tmp_path / 'data'
            self.store = Connection()
            events.append('opened')

        def close(self):
            self.store.close()

    def backup(runtime, name, archive, passphrase):
        assert isinstance(runtime, Runtime) and name == 'alpha'
        assert archive == tmp_path / 'archive' and passphrase == PASSWORD
        events.append('backup')
        raise StoreError('PROFILE_BUSY', PASSWORD)

    def forbidden(args):
        pytest.fail('Archive backup must not also open the generic Store connection.')

    monkeypatch.setattr(daeri.application.runtime, 'Runtime', Runtime)
    monkeypatch.setattr(profile_portability, 'backup_profile', backup)
    monkeypatch.setattr(cli, '_with_store', forbidden)
    code, result, stderr = invoke('backup', 'alpha', str(tmp_path / 'archive'), '--password-file', str(path))
    assert code == 1 and result['error']['code'] == 'PROFILE_BUSY'
    assert events == ['opened', 'backup', 'closed'] and stderr == ''


def test_restore_confirmation_uses_only_one_store_and_closes_it(tmp_path, monkeypatch, invoke):
    import daeri.infrastructure.storage

    path = password_file(tmp_path)
    events = []

    class Store:
        def __init__(self, data_dir):
            assert data_dir == tmp_path / 'data'
            events.append('opened')

        def close(self):
            events.append('closed')

    def restore(store, archive, passphrase, confirmation):
        assert isinstance(store, Store) and archive == tmp_path / 'archive'
        assert passphrase == PASSWORD and confirmation == 'confirmed-token'
        events.append('restored')
        return {'profile_id': 'profile_restored', 'restored': True}

    monkeypatch.setattr(daeri.infrastructure.storage, 'Store', Store)
    monkeypatch.setattr(profile_portability, 'restore_profile', restore)
    code, result, stderr = invoke('restore', str(tmp_path / 'archive'), '--confirm', 'confirmed-token',
                                  '--password-file', str(path))
    assert code == 0 and result == {'profile_id': 'profile_restored', 'restored': True}
    assert events == ['opened', 'restored', 'closed'] and stderr == ''


def test_terminal_password_is_hidden_and_never_placed_in_output(tmp_path, monkeypatch, invoke):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, 'stdin', Terminal())
    monkeypatch.setattr(getpass, 'getpass', lambda prompt: PASSWORD)

    def inspect(archive, passphrase):
        assert passphrase == PASSWORD
        return {'confirmation_token': 'token'}

    monkeypatch.setattr(profile_portability, 'inspect_backup', inspect)
    code, result, stderr = invoke('restore', str(tmp_path / 'archive'))
    assert code == 0 and result == {'confirmation_token': 'token'} and stderr == ''


def test_archive_commands_reject_password_values_in_arguments(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['daeri', '--data-dir', str(tmp_path / 'data'), 'profile',
                                    'restore', 'archive', '--password', PASSWORD])
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err
    assert json.loads(captured.out)['error']['code'] == 'INVALID_INPUT'
    assert not (tmp_path / 'data').exists()


def test_cli_backup_preview_and_confirmed_restore_preserve_profile_identity(tmp_path, invoke):
    from daeri.infrastructure.storage import Store

    source = Store(tmp_path / 'source')
    try:
        original = source.put_profile('alpha', core_context=['example context'], embedding={'provider': 'keyword_only'})
    finally:
        source.close()
    password = password_file(tmp_path)
    archive = tmp_path / 'profile.daeri'
    options = ('--password-file', str(password))
    code, backed_up, _ = invoke('backup', 'alpha', str(archive), *options, data_dir=tmp_path / 'source')
    assert code == 0 and archive.is_file() and backed_up['profile_id'] == original['profile_id']
    code, preview, _ = invoke('restore', str(archive), *options)
    assert code == 0 and preview['profile_id'] == original['profile_id']
    assert not (tmp_path / 'data').exists()
    code, restored, _ = invoke('restore', str(archive), '--confirm', preview['confirmation_token'], *options)
    assert code == 0 and restored['restored'] is True
    destination = Store(tmp_path / 'data')
    try:
        assert destination.get_profile('alpha') == original
    finally:
        destination.close()
