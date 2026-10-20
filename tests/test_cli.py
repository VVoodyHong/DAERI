import json
import subprocess
import sys


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "daeri.cli", *args],
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_check_reports_real_storage_features_without_creating_user_data(tmp_path):
    data_dir = tmp_path / "user-data"
    result = run_cli("--data-dir", str(data_dir), "check")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ready"] is True
    assert report["sqlite_fts5"] is True
    assert not data_dir.exists()


def test_profile_round_trip_across_processes(tmp_path):
    data_dir = str(tmp_path / "user-data")
    created = run_cli("--data-dir", data_dir, "profile", "create", "service-prod", "--context", "UTC 기준 운영")
    assert created.returncode == 0, created.stderr
    profile = json.loads(created.stdout)
    assert profile["name"] == "service-prod"
    shown = run_cli("--data-dir", data_dir, "profile", "show", profile["profile_id"])
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["core_context"] == ["UTC 기준 운영"]
    listed = run_cli("--data-dir", data_dir, "profile", "list")
    assert listed.returncode == 0, listed.stderr
    assert [p["name"] for p in json.loads(listed.stdout)] == ["service-prod"]


def test_profile_error_does_not_leak_traceback(tmp_path):
    result = run_cli("--data-dir", str(tmp_path), "profile", "show", "missing")
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert json.loads(result.stdout)["error"]["code"] == "NOT_FOUND"


def test_profile_context_known_credentials_are_redacted_before_storage(tmp_path):
    result = run_cli("--data-dir", str(tmp_path), "profile", "create", "local",
                     "--context", "Authorization: Bearer CLI_PRIVATE_TOKEN")
    assert result.returncode == 0
    assert "CLI_PRIVATE_TOKEN" not in result.stdout
    for path in (tmp_path / "profiles").glob("*.json"):
        assert "CLI_PRIVATE_TOKEN" not in path.read_text()


def test_profile_import_export_is_portable_and_rejects_plaintext_secrets(tmp_path):
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps({'name': 'imported', 'core_context': ['UTC'], 'resources': [], 'memory_policy': {}}))
    result = run_cli('--data-dir', str(tmp_path / 'data'), 'profile', 'import', str(path))
    assert result.returncode == 0, result.stdout + result.stderr
    exported = run_cli('--data-dir', str(tmp_path / 'data'), 'profile', 'export', 'imported')
    value = json.loads(exported.stdout)
    assert value['name'] == 'imported'
    assert 'profile_id' not in value and 'revision' not in value
    path.write_text('{"name":"bad","core_context":["token=IMPORT_PRIVATE_SENTINEL"]}')
    invalid = run_cli('--data-dir', str(tmp_path / 'data'), 'profile', 'import', str(path))
    assert invalid.returncode != 0
    assert 'IMPORT_PRIVATE_SENTINEL' not in invalid.stdout + invalid.stderr


def test_catalog_and_template_are_local_and_do_not_create_user_store(tmp_path):
    directory = tmp_path / 'absent'
    catalog = run_cli('--data-dir', str(directory), 'resource', 'catalog')
    assert catalog.returncode == 0, catalog.stdout + catalog.stderr
    assert 'source-gitlab' in json.loads(catalog.stdout)
    template = run_cli('--data-dir', str(directory), 'resource', 'template', 'source-gitlab',
                       '--profile', 'service', '--instance', 'source-one')
    assert template.returncode == 0, template.stdout + template.stderr
    value = json.loads(template.stdout)
    assert value['resources'][0]['policy'] == {}
    assert not directory.exists()


def test_auth_set_requires_explicit_secure_input_and_never_echoes_secret(tmp_path):
    result = run_cli('--data-dir', str(tmp_path), 'auth', 'set', 'credential:local')
    assert result.returncode != 0
    assert json.loads(result.stdout)['error']['code'] == 'AUTHENTICATION_REQUIRED'


def test_memory_status_does_not_prepare_model_implicitly(tmp_path):
    directory = tmp_path / 'data'
    result = run_cli('--data-dir', str(directory), 'memory', 'status')
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value['model_prepared'] is False
    assert value['vector_ready'] is False
    assert not (directory / 'models').exists()


def test_argument_error_does_not_echo_mistaken_secret_argument():
    result = run_cli('auth', 'set', 'credential:local', '--password', 'ARGUMENT_PRIVATE_SENTINEL')
    assert result.returncode == 2
    assert 'ARGUMENT_PRIVATE_SENTINEL' not in result.stdout + result.stderr


def test_auth_standard_input_is_stored_without_returning_value(monkeypatch):
    from daeri.cli import _auth
    from types import SimpleNamespace
    from io import StringIO
    stored = {}
    class FakeCredentials:
        def set(self, reference, value): stored[reference] = value
    monkeypatch.setattr('daeri.credentials.Credentials', FakeCredentials)
    monkeypatch.setattr('sys.stdin', StringIO('AUTH_PRIVATE_SENTINEL\n'))
    result = _auth(SimpleNamespace(auth_command='set', stdin=True, reference='credential:test'))
    assert stored == {'credential:test': 'AUTH_PRIVATE_SENTINEL'}
    assert result == {'reference': 'credential:test', 'stored': True}
    assert 'AUTH_PRIVATE_SENTINEL' not in json.dumps(result)


def test_auth_prompt_never_falls_back_to_echoed_input(monkeypatch):
    from daeri.cli import _auth
    from daeri.storage import StoreError
    from types import SimpleNamespace
    import getpass
    import warnings
    class Terminal:
        def isatty(self): return True
    monkeypatch.setattr('sys.stdin', Terminal())
    def insecure_prompt(*args, **kwargs):
        warnings.warn('echo would be enabled', getpass.GetPassWarning)
        raise AssertionError('Must not read echoed input')
    monkeypatch.setattr('getpass.getpass', insecure_prompt)
    with __import__('pytest').raises(StoreError) as caught:
        _auth(SimpleNamespace(auth_command='set', stdin=False, reference='credential:test'))
    assert caught.value.code == 'AUTHENTICATION_REQUIRED'


def test_profile_json_is_readable_through_a_non_unicode_host_pipe(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONIOENCODING', 'ascii')
    result = run_cli('--data-dir', str(tmp_path), 'profile', 'create', 'service',
                     '--context', '한국어 운영 맥락')
    assert result.returncode == 0, result.stderr
    assert result.stdout.isascii()
    assert json.loads(result.stdout)['core_context'] == ['한국어 운영 맥락']
