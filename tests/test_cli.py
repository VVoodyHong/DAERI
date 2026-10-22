import json
import subprocess
import sys

import pytest


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "daeri.cli", *args],
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_check_reports_real_storage_features_without_creating_user_data(tmp_path):
    import importlib.metadata
    data_dir = tmp_path / "user-data"
    result = run_cli("--data-dir", str(data_dir), "check")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ready"] is True
    assert report["version"] == importlib.metadata.version('daeri-agent')
    assert report["sqlite_fts5"] is True
    assert not data_dir.exists()


def test_setup_catalog_does_not_create_user_data(tmp_path):
    data_dir = tmp_path / 'user-data'
    result = run_cli('--data-dir', str(data_dir), 'setup', 'catalog')
    assert result.returncode == 0, result.stdout
    groups = json.loads(result.stdout)['groups']
    assert {group['type'] for group in groups} == {'DB', 'Cache', 'Infra', 'Source', 'Document', 'Issue'}
    assert not data_dir.exists()


def test_setup_company_api_adapter_selection_round_trips_through_cli(tmp_path):
    from pathlib import Path
    from daeri.setup_yaml import read_yaml, write_yaml
    prefix = ('--data-dir', str(tmp_path / 'private'), 'setup')
    catalog = json.loads(run_cli(*prefix, 'catalog').stdout)
    assert [item['adapter_id'] for item in catalog['embedding_adapters']] == ['openai_compatible']
    draft = run_cli(*prefix, 'draft', 'service', '--embedding', 'api',
                    '--embedding-adapter', 'openai_compatible')
    assert draft.returncode == 0, draft.stdout + draft.stderr
    path = Path(json.loads(draft.stdout)['path'])
    value = read_yaml(path)
    assert value['embedding']['provider'] == 'api'
    assert value['embedding']['adapter'] == 'openai_compatible'
    value['embedding'].update(endpoint='https://embedding.example.test/v1/embeddings',
                              model='company-model', allow_remote=False)
    path.write_text(write_yaml(value), encoding='utf-8')
    validated = run_cli(*prefix, 'validate', 'service')
    assert validated.returncode == 0, validated.stdout
    token = json.loads(validated.stdout)['confirmation_token']
    applied = run_cli(*prefix, 'apply', 'service', '--confirm', token)
    assert applied.returncode == 0, applied.stdout
    assert json.loads(applied.stdout)['embedding']['adapter'] == 'openai_compatible'


def test_setup_company_api_without_adapter_requires_selection_before_apply(tmp_path):
    prefix = ('--data-dir', str(tmp_path / 'private'), 'setup')
    draft = run_cli(*prefix, 'draft', 'service', '--embedding', 'api')
    assert draft.returncode == 0, draft.stdout + draft.stderr
    validated = run_cli(*prefix, 'validate', 'service')
    assert validated.returncode == 1
    result = json.loads(validated.stdout)
    assert not result['complete'] and result['confirmation_token'] is None


@pytest.mark.parametrize('empty_directory', [False, True])
def test_setup_status_treats_a_missing_draft_as_a_normal_first_run(tmp_path, empty_directory):
    data_dir = tmp_path / 'Application Support' / 'DAERI'
    if empty_directory:
        (data_dir / 'setup' / 'eu-mcp-prd').mkdir(parents=True)
    before = set(tmp_path.rglob('*'))
    result = run_cli('--data-dir', str(data_dir), 'setup', 'status', 'eu-mcp-prd')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {'profile': 'eu-mcp-prd', 'state': 'new', 'path': None}
    assert set(tmp_path.rglob('*')) == before


def test_setup_status_resumes_readable_draft_without_changing_its_contents(tmp_path):
    from pathlib import Path
    prefix = ('--data-dir', str(tmp_path / 'private'), 'setup')
    created = run_cli(*prefix, 'draft', 'service', '--resource', 'primary=db-postgres')
    path = Path(json.loads(created.stdout)['path'])
    path.write_text('profile: service\nresources: [unfinished', encoding='utf-8')
    before = {item: item.read_bytes() for item in path.parent.iterdir()}
    result = run_cli(*prefix, 'status', 'service')
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {'profile': 'service', 'state': 'resume', 'path': str(path)}
    assert {item: item.read_bytes() for item in path.parent.iterdir()} == before


@pytest.mark.parametrize('obstruction', ['partial', 'directory_as_file', 'file_as_directory'])
def test_setup_status_preserves_broken_drafts_and_returns_a_safe_error(tmp_path, obstruction):
    data_dir = tmp_path / 'private'
    directory = data_dir / 'setup' / 'service'
    if obstruction == 'file_as_directory':
        data_dir.mkdir()
        (data_dir / 'setup').write_text('PRIVATE_SENTINEL')
    else:
        directory.mkdir(parents=True)
        if obstruction == 'partial':
            (directory / 'setup.yaml').write_text('PRIVATE_SENTINEL')
        else:
            (directory / 'setup.yaml').mkdir()
            (directory / 'draft.json').write_text('PRIVATE_SENTINEL')
    before = {path: path.read_bytes() for path in data_dir.rglob('*') if path.is_file()}
    result = run_cli('--data-dir', str(data_dir), 'setup', 'status', 'service')
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'INVALID_INPUT'
    assert 'PRIVATE_SENTINEL' not in result.stdout + result.stderr
    assert {path: path.read_bytes() for path in data_dir.rglob('*') if path.is_file()} == before


def test_setup_status_validates_profile_before_inspecting_paths(tmp_path):
    data_dir = tmp_path / 'private'
    result = run_cli('--data-dir', str(data_dir), 'setup', 'status', '../escape')
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'INVALID_INPUT'
    assert not data_dir.exists()


def test_setup_yaml_draft_validate_and_apply_across_processes(tmp_path):
    from pathlib import Path
    from daeri.setup_yaml import read_yaml, write_yaml
    prefix = ('--data-dir', str(tmp_path / 'private'), 'setup')
    draft = run_cli(*prefix, 'draft', 'service', '--resource', 'primary=db-postgres', '--embedding', 'keyword_only')
    assert draft.returncode == 0, draft.stdout
    path = Path(json.loads(draft.stdout)['path'])
    invalid = run_cli(*prefix, 'validate', 'service')
    assert invalid.returncode == 1 and json.loads(invalid.stdout)['issues']
    value = read_yaml(path)
    value['resources']['primary']['inputs'].update(host='db.example.test', database='service',
        username='reader')
    path.write_text(write_yaml(value), encoding='utf-8')
    validated = run_cli(*prefix, 'validate', 'service')
    assert validated.returncode == 0, validated.stdout
    token = json.loads(validated.stdout)['confirmation_token']
    applied = run_cli(*prefix, 'apply', 'service', '--confirm', token)
    assert applied.returncode == 0, applied.stdout
    assert json.loads(applied.stdout)['revision'] == 1
    assert json.loads(run_cli('--data-dir', str(tmp_path / 'private'), 'profile', 'show', 'service').stdout)['resources']


def test_setup_duplicate_instance_arguments_are_rejected(tmp_path):
    result = run_cli('--data-dir', str(tmp_path / 'private'), 'setup', 'draft', 'service',
                     '--resource', 'primary=db-postgres', '--resource', 'primary=cache-redis')
    assert result.returncode != 0
    assert json.loads(result.stdout)['error']['code'] == 'INVALID_INPUT'


@pytest.mark.parametrize('recipes,expected', [
    (['db-postgres'] * 2 + ['cache-redis'] + ['issue-jira'] * 3,
     ['db', 'db-2', 'cache', 'issue', 'issue-2', 'issue-3']),
    (['issue-jira'] * 7, ['issue'] + [f'issue-{number}' for number in range(2, 8)]),
])
def test_setup_unnamed_resource_arguments_use_category_names_and_allow_repeated_products(tmp_path, recipes, expected):
    from pathlib import Path
    prefix = ('--data-dir', str(tmp_path / 'private'), 'setup', 'draft', 'service')
    arguments = tuple(part for recipe in recipes for part in ('--resource', recipe)) + ('--embedding', 'keyword_only')
    draft = run_cli(*prefix, *arguments)
    assert draft.returncode == 0, draft.stdout
    result = json.loads(draft.stdout)
    assert result['resources'] == expected
    path = Path(result['path'])
    original = path.read_bytes()
    assert run_cli(*prefix, *arguments).returncode == 0
    assert path.read_bytes() == original


def test_profile_round_trip_across_processes(tmp_path):
    data_dir = str(tmp_path / "user-data")
    created = run_cli("--data-dir", data_dir, "profile", "create", "service-prod", "--context", "UTC 기준 운영")
    assert created.returncode == 0, created.stderr
    profile = json.loads(created.stdout)
    assert profile["name"] == "service-prod"
    assert profile["retention_days"] is None
    assert profile["long_term_memory_days"] is None
    shown = run_cli("--data-dir", data_dir, "profile", "show", profile["profile_id"])
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["core_context"] == ["UTC 기준 운영"]
    listed = run_cli("--data-dir", data_dir, "profile", "list")
    assert listed.returncode == 0, listed.stderr
    assert [p["name"] for p in json.loads(listed.stdout)] == ["service-prod"]


@pytest.mark.parametrize('value,expected', [('unlimited', None), ('90', 90)])
def test_profile_creation_accepts_explicit_memory_retention(tmp_path, value, expected):
    result = run_cli('--data-dir', str(tmp_path), 'profile', 'create', 'service',
                     '--long-term-memory-days', value)
    assert result.returncode == 0, result.stdout + result.stderr
    profile = json.loads(result.stdout)
    assert profile['long_term_memory_days'] == expected
    assert profile['retention_days'] is None


@pytest.mark.parametrize('value,expected', [('unlimited', None), ('30', 30)])
def test_profile_creation_accepts_explicit_body_retention(tmp_path, value, expected):
    result = run_cli('--data-dir', str(tmp_path), 'profile', 'create', 'service', '--retention-days', value)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['retention_days'] == expected


def test_repeated_profile_creation_preserves_existing_settings(tmp_path):
    arguments = ('--data-dir', str(tmp_path), 'profile')
    first = run_cli(*arguments, 'create', 'service', '--context', '기존 운영 규칙',
                    '--retention-days', '15', '--long-term-memory-days', '365')
    assert first.returncode == 0
    repeated = run_cli(*arguments, 'create', 'service')
    assert repeated.returncode != 0
    assert json.loads(repeated.stdout)['error']['code'] == 'PROFILE_ALREADY_EXISTS'
    shown = run_cli(*arguments, 'show', 'service')
    assert json.loads(shown.stdout) == json.loads(first.stdout)


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


def test_auth_set_without_a_user_terminal_returns_normal_input_required_state(tmp_path):
    result = run_cli('--data-dir', str(tmp_path), 'auth', 'set', 'credential:local')
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['status'] == 'input_required'
    assert report['reference'] == 'credential:local' and report['stored'] is False
    assert '터미널' in report['message'] and 'error' not in report
    assert not list(tmp_path.iterdir())


def test_auth_set_checks_the_reference_before_requesting_input():
    result = run_cli('auth', 'set', 'credential:../invalid')
    assert result.returncode != 0
    assert json.loads(result.stdout)['error']['code'] == 'INVALID_INPUT'


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
    monkeypatch.setattr('daeri.credentials.Credentials.set',
                        lambda self, reference, value: stored.update({reference: value}))
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


@pytest.mark.parametrize('directory', ['relative-data', '../relative-data'])
def test_environment_data_directory_rejects_ambiguous_relative_paths(tmp_path, monkeypatch, directory):
    working = tmp_path / 'working'
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setenv('DAERI_DATA_DIR', directory)
    result = run_cli('profile', 'list')
    assert result.returncode != 0
    assert json.loads(result.stdout)['error']['code'] == 'INVALID_INPUT'
    assert not (working / directory).exists()
    explicit = run_cli('--data-dir', str(tmp_path / 'explicit'), 'profile', 'list')
    assert explicit.returncode == 0
    assert json.loads(explicit.stdout) == []


def test_environment_data_directory_expands_current_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('DAERI_DATA_DIR', '~/private-data')
    result = run_cli('profile', 'list')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'private-data/daeri.sqlite3').is_file()
