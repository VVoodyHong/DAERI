import json
from pathlib import Path

import pytest

from daeri.credentials import Credentials
from daeri.onboarding import apply_draft, create_draft, validate_draft
from daeri.setup_yaml import read_yaml, write_yaml
from daeri.storage import Store, StoreError


class Vault:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def get_password(self, service, name):
        return self.values.get(name)

    def set_password(self, service, name, value):
        self.writes.append(name)
        self.values[name] = value

    def delete_password(self, service, name):
        self.values.pop(name, None)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / 'data')
    yield value
    value.close()


def edit(draft, change):
    path = Path(draft['path'])
    value = read_yaml(path)
    change(value)
    path.write_text(write_yaml(value), encoding='utf-8')


def database_draft(store):
    draft = create_draft(store, 'production', recipes=['db-postgres'], embedding='keyword_only')
    edit(draft, lambda value: value['resources']['db']['inputs'].update(
        host='db.example.test', database='service', username='reader'))
    return draft


def approve(store, vault):
    result = validate_draft(store, 'production')
    assert result['valid'], result['issues']
    return apply_draft(store, 'production', result['confirmation_token'], credentials=Credentials(backend=vault))


def test_selected_connections_generate_editable_credentials_with_registered_comments(store):
    draft = create_draft(store, 'production', recipes=['db-postgres', 'cache-redis', 'infra-aws',
        'source-gitlab', 'document-confluence', 'issue-jira'], embedding='api',
        embedding_adapter='openai_compatible')
    value = read_yaml(draft['path'])
    assert value['resources']['db']['credentials'] == {'password': None}
    assert value['resources']['cache']['credentials'] == {'password': None}
    assert 'credentials' not in value['resources']['infra']
    for instance in ('source', 'document', 'issue'):
        assert value['resources'][instance]['credentials'] == {'token': None}
    assert value['embedding']['credentials'] == {'api_key': None}
    text = Path(draft['path']).read_text(encoding='utf-8')
    assert '# ' in text and 'credentials:' in text


def test_credentials_are_previewed_as_references_and_imported_only_when_applied(store):
    draft = database_draft(store)
    secret = 'PRIVATE_PASSWORD_!@:/#'
    edit(draft, lambda value: value['resources']['db'].update(credentials={'password': secret}))
    original = Path(draft['path']).read_bytes()
    result = validate_draft(store, 'production')
    assert result['valid'], result['issues']
    assert result['preview']['resources']['db']['credentials'] == {
        'password': {'ref': 'credential:production.db.database_password'}}
    assert result['preview']['changes']['credentials'] == ['resources.db.credentials.password']
    assert secret not in json.dumps(result)
    assert Path(draft['path']).read_bytes() == original
    vault = Vault()
    profile = apply_draft(store, 'production', result['confirmation_token'], credentials=Credentials(backend=vault))
    assert vault.values == {'production.db.database_password': secret}
    assert secret not in json.dumps(profile)
    assert read_yaml(draft['path'])['resources']['db']['credentials'] == {
        'password': {'ref': 'credential:production.db.database_password'}}
    assert secret not in Path(draft['path']).read_text(encoding='utf-8')
    for path in store.data_dir.rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()


def test_changing_only_password_invalidates_confirmation_and_updates_profile_revision(store):
    draft = database_draft(store)
    edit(draft, lambda value: value['resources']['db'].update(credentials={'password': 'FIRST_PRIVATE'}))
    old_token = validate_draft(store, 'production')['confirmation_token']
    edit(draft, lambda value: value['resources']['db']['credentials'].update(password='SECOND_PRIVATE'))
    vault = Vault()
    with pytest.raises(StoreError, match='REVISION_CONFLICT'):
        apply_draft(store, 'production', old_token, credentials=Credentials(backend=vault))
    assert vault.values == {}
    profile = approve(store, vault)
    edit(draft, lambda value: value['resources']['db']['credentials'].update(password='THIRD_PRIVATE'))
    updated = approve(store, vault)
    assert updated['revision'] == profile['revision'] + 1
    assert vault.values['production.db.database_password'] == 'THIRD_PRIVATE'
    repeat = approve(store, vault)
    assert repeat['revision'] == updated['revision']
    edit(draft, lambda value: value['resources']['db']['credentials'].update(password='THIRD_PRIVATE'))
    assert approve(store, vault)['revision'] == updated['revision']


@pytest.mark.parametrize('credential', [None, {'ref': 'credential:shared.database'}, {'ref': 'env:DB_PASSWORD'}])
def test_missing_null_and_reference_credentials_reuse_existing_registration(store, credential):
    draft = database_draft(store)
    edit(draft, lambda value: value['resources']['db'].update(credentials={'password': credential}))
    vault = Vault({'production.db.database_password': 'kept-private'})
    profile = approve(store, vault)
    reference = credential['ref'] if credential else 'credential:production.db.database_password'
    assert profile['resources'][0]['connection']['auth_env']['DATABASE_PASSWORD'] == reference
    assert vault.writes == []
    assert vault.values == {'production.db.database_password': 'kept-private'}
    edit(draft, lambda value: value['resources']['db'].pop('credentials'))
    assert approve(store, vault)['revision'] == profile['revision']


@pytest.mark.parametrize('credential', ['', 123, True, [], {'value': 'PRIVATE_SENTINEL'},
    {'ref': 'PRIVATE_SENTINEL'}, {'ref': 'credential:shared', 'extra': 'PRIVATE_SENTINEL'},
    'private\nline', 'private\tline', 'private\x00line', 'x' * 16385])
def test_invalid_credentials_report_only_registered_field_paths(store, credential):
    draft = database_draft(store)
    edit(draft, lambda value: value['resources']['db'].update(credentials={'password': credential}))
    result = validate_draft(store, 'production')
    assert not result['valid'] and result['confirmation_token'] is None
    assert result['issues'][0]['field'] == 'resources.db.credentials.password'
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_plaintext_cannot_replace_environment_reference(store):
    draft = database_draft(store)
    edit(draft, lambda value: value['resources']['db'].update(
        credentials={'password': {'ref': 'env:DB_PASSWORD'}}))
    vault = Vault()
    approve(store, vault)
    edit(draft, lambda value: value['resources']['db']['credentials'].update(password='PRIVATE_SENTINEL'))
    result = validate_draft(store, 'production')
    assert not result['valid']
    assert result['issues'][0]['field'] == 'resources.db.credentials.password'
    assert 'PRIVATE_SENTINEL' not in json.dumps(result)


def test_company_api_key_uses_same_yaml_and_stays_out_of_embedding_configuration(store):
    draft = create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    edit(draft, lambda value: value['embedding'].update(
        endpoint='https://embedding.example.test/v1/embeddings', model='company-model',
        allow_remote=True, credentials={'api_key': 'sk-PRIVATE_EMBEDDING_12345'}))
    result = validate_draft(store, 'production')
    assert result['valid'], result['issues']
    assert 'sk-PRIVATE_EMBEDDING_12345' not in json.dumps(result)
    assert result['preview']['embedding']['credentials'] == {
        'api_key': {'ref': 'credential:production.embedding'}}
    vault = Vault()
    profile = apply_draft(store, 'production', result['confirmation_token'], credentials=Credentials(backend=vault))
    assert profile['embedding']['credential_ref'] == 'credential:production.embedding'
    assert 'credentials' not in profile['embedding']
    assert vault.values == {'production.embedding': 'sk-PRIVATE_EMBEDDING_12345'}
    assert read_yaml(draft['path'])['embedding']['credentials'] == {
        'api_key': {'ref': 'credential:production.embedding'}}


@pytest.mark.parametrize('remove_from_draft', [False, True])
def test_omitted_embedding_preserves_existing_api_settings_and_revision(store, remove_from_draft):
    profile = store.put_profile('production', embedding={
        'provider': 'api', 'adapter': 'openai_compatible',
        'endpoint': 'https://embedding.example.test/v1/embeddings', 'model': 'company-model',
        'credential_ref': 'credential:shared.embedding', 'allow_remote': True})
    options = {'embedding': 'api', 'embedding_adapter': 'openai_compatible'} if remove_from_draft else {}
    draft = create_draft(store, 'production', **options)
    if remove_from_draft:
        edit(draft, lambda value: value.pop('embedding'))

    checked = validate_draft(store, 'production')
    assert checked['valid'], checked['issues']
    assert checked['preview']['embedding'] == profile['embedding']
    assert checked['preview']['changes']['changed'] is False
    vault = Vault({'shared.embedding': 'kept-private'})
    saved = apply_draft(store, 'production', checked['confirmation_token'],
                        credentials=Credentials(backend=vault))
    assert saved == profile
    assert 'embedding' not in read_yaml(draft['path'])
    assert vault.writes == []
    assert vault.values == {'shared.embedding': 'kept-private'}


@pytest.mark.parametrize('reference', [None, 123, False, []])
def test_plaintext_embedding_key_requires_a_valid_destination_reference(store, reference):
    draft = create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    edit(draft, lambda value: value['embedding'].update(
        endpoint='https://embedding.example.test/v1/embeddings', model='company-model',
        credential_ref=reference, credentials={'api_key': 'PRIVATE_SENTINEL'}))
    result = validate_draft(store, 'production')
    assert not result['valid'] and 'PRIVATE_SENTINEL' not in json.dumps(result)
    assert result['issues'][0]['field'] == 'embedding.credentials.api_key'


def test_embedding_reference_override_can_fill_missing_public_reference(store):
    draft = create_draft(store, 'production', embedding='api', embedding_adapter='openai_compatible')
    edit(draft, lambda value: value['embedding'].update(
        endpoint='https://embedding.example.test/v1/embeddings', model='company-model',
        credential_ref=None, credentials={'api_key': {'ref': 'credential:company.embedding'}}))
    profile = approve(store, Vault())
    assert profile['embedding']['credential_ref'] == 'credential:company.embedding'


def test_jira_authentication_mode_selects_current_token_reference(store):
    draft = create_draft(store, 'production', recipes=['issue-jira'], embedding='keyword_only')
    edit(draft, lambda value: value['resources']['issue']['inputs'].update(
        server='https://issues.example.test', auth_type='personal_token'))
    edit(draft, lambda value: value['resources']['issue'].update(credentials={'token': 'PRIVATE_PAT'}))
    vault = Vault()
    profile = approve(store, vault)
    assert profile['resources'][0]['connection']['auth_env'] == {
        'JIRA_PERSONAL_TOKEN': 'credential:production.issue.jira_personal_token'}
    assert vault.values == {'production.issue.jira_personal_token': 'PRIVATE_PAT'}


def test_same_reference_with_conflicting_values_fails_before_native_writes(store):
    draft = create_draft(store, 'production', recipes=['db-postgres', 'cache-redis'], embedding='keyword_only')
    edit(draft, lambda value: value['resources']['db']['inputs'].update(
        host='db.example.test', database='service', username='reader'))
    edit(draft, lambda value: value['resources']['cache']['inputs'].update(host='cache.example.test'))
    edit(draft, lambda value: [block.update(credentials={'password': {'ref': 'credential:shared'}})
                              for block in value['resources'].values()])
    vault = Vault()
    approve(store, vault)
    edit(draft, lambda value: value['resources']['db']['credentials'].update(password='DB_PRIVATE'))
    edit(draft, lambda value: value['resources']['cache']['credentials'].update(password='CACHE_PRIVATE'))
    result = validate_draft(store, 'production')
    assert not result['valid']
    assert all('PRIVATE' not in issue['message'] for issue in result['issues'])
    assert vault.writes == []


def test_native_failure_preserves_active_profile_and_user_yaml_for_retry(store):
    draft = database_draft(store)
    vault = Vault({'production.db.database_password': 'OLD_PRIVATE'})
    original_profile = approve(store, vault)
    edit(draft, lambda value: value['resources']['db']['credentials'].update(password='NEW_PRIVATE'))
    original_yaml = Path(draft['path']).read_bytes()
    original_meta = Path(draft['path']).with_name('draft.json').read_bytes()

    class Locked(Vault):
        def set_password(self, service, name, value):
            raise RuntimeError('NEW_PRIVATE')

    locked = Locked(vault.values)
    with pytest.raises(StoreError) as raised:
        apply_draft(store, 'production', validate_draft(store, 'production')['confirmation_token'],
                    credentials=Credentials(backend=locked))
    assert 'NEW_PRIVATE' not in str(raised.value)
    assert Path(draft['path']).read_bytes() == original_yaml
    assert Path(draft['path']).with_name('draft.json').read_bytes() == original_meta
    assert store.get_profile('production') == original_profile


def test_file_failure_restores_native_value_and_preserves_retryable_draft(store, monkeypatch):
    from daeri import onboarding
    draft = database_draft(store)
    vault = Vault({'production.db.database_password': 'OLD_PRIVATE'})
    original_profile = approve(store, vault)
    edit(draft, lambda value: value['resources']['db']['credentials'].update(password='NEW_PRIVATE'))
    original_yaml = Path(draft['path']).read_bytes()
    original_meta = Path(draft['path']).with_name('draft.json').read_bytes()
    original_write = onboarding._write

    def unavailable(path, text):
        if path.name == 'draft.json':
            raise StoreError('STORE_UNAVAILABLE', 'Simulated metadata failure.')
        return original_write(path, text)

    monkeypatch.setattr(onboarding, '_write', unavailable)
    with pytest.raises(StoreError):
        apply_draft(store, 'production', validate_draft(store, 'production')['confirmation_token'],
                    credentials=Credentials(backend=vault))
    assert Path(draft['path']).read_bytes() == original_yaml
    assert Path(draft['path']).with_name('draft.json').read_bytes() == original_meta
    assert vault.values['production.db.database_password'] == 'OLD_PRIVATE'
    assert store.get_profile('production') == original_profile


@pytest.mark.parametrize('missing_yaml', [False, True])
def test_commit_failure_restores_yaml_metadata_profile_and_native_value(store, missing_yaml):
    import sqlite3
    draft = database_draft(store)
    vault = Vault({'production.db.database_password': 'OLD_PRIVATE'})
    original_profile = approve(store, vault)
    edit(draft, lambda value: value['resources']['db']['credentials'].update(password='NEW_PRIVATE'))
    yaml_before = Path(draft['path']).read_bytes()
    metadata = Path(draft['path']).with_name('draft.json')
    metadata_before = metadata.read_bytes()
    token = validate_draft(store, 'production')['confirmation_token']
    connection = store._db

    class FailedCommit:
        def __getattr__(self, name):
            return getattr(connection, name)

        def commit(self):
            if missing_yaml:
                Path(draft['path']).unlink()
            raise sqlite3.OperationalError('COMMIT_FAILURE')

    store._db = FailedCommit()
    try:
        with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
            apply_draft(store, 'production', token, credentials=Credentials(backend=vault))
    finally:
        store._db = connection
    assert not connection.in_transaction
    assert vault.values['production.db.database_password'] == 'OLD_PRIVATE'
    assert Path(draft['path']).read_bytes() == yaml_before
    assert metadata.read_bytes() == metadata_before
    assert store.get_profile('production') == original_profile


def test_concurrent_confirmed_applies_keep_the_winners_native_value(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    draft = database_draft(store)
    edit(draft, lambda value: value['resources']['db'].update(credentials={'password': 'WINNER_PRIVATE'}))
    token = validate_draft(store, 'production')['confirmation_token']
    vault, barrier = Vault(), Barrier(2)
    other = Store(store.data_dir)

    def apply(selected_store):
        barrier.wait(timeout=5)
        try:
            return apply_draft(selected_store, 'production', token, credentials=Credentials(backend=vault))
        except StoreError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, (store, other)))
    finally:
        other.close()
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 'REVISION_CONFLICT' in results
    assert vault.values == {'production.db.database_password': 'WINNER_PRIVATE'}
    assert vault.writes == ['production.db.database_password']
    assert store.get_profile('production')['revision'] == 1


def test_yaml_edit_during_native_prompt_is_preserved_for_the_next_confirmed_apply(store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    draft = database_draft(store)
    approve(store, Vault())
    edit(draft, lambda value: value['resources']['db'].update(credentials={'password': 'FIRST_PRIVATE'}))
    token = validate_draft(store, 'production')['confirmation_token']
    first_written, release_first, second_started, second_validated = (Event() for _ in range(4))

    class PausedVault(Vault):
        def set_password(self, service, name, value):
            super().set_password(service, name, value)
            if value == 'FIRST_PRIVATE':
                first_written.set()
                assert release_first.wait(timeout=5)

    vault = PausedVault({'production.db.database_password': 'ORIGINAL_PRIVATE'})
    other = Store(store.data_dir)

    def first_apply():
        try:
            return apply_draft(store, 'production', token, credentials=Credentials(backend=vault))
        except StoreError as error:
            return error.code

    def second_apply():
        second_started.set()
        result = validate_draft(other, 'production')
        second_validated.set()
        return apply_draft(other, 'production', result['confirmation_token'], credentials=Credentials(backend=vault))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_apply)
            try:
                assert first_written.wait(timeout=5)
                edit(draft, lambda value: value['resources']['db']['credentials'].update(password='SECOND_PRIVATE'))
                second = pool.submit(second_apply)
                assert second_started.wait(timeout=5)
                assert not second_validated.wait(timeout=0.05)
            finally:
                release_first.set()
            assert first.result(timeout=5) == 'REVISION_CONFLICT'
            assert second.result(timeout=5)['revision'] == 2
    finally:
        release_first.set()
        other.close()
    assert vault.values['production.db.database_password'] == 'SECOND_PRIVATE'
    assert read_yaml(draft['path'])['resources']['db']['credentials'] == {
        'password': {'ref': 'credential:production.db.database_password'}}


def test_nested_apply_rejects_before_any_native_update(store):
    draft = database_draft(store)
    edit(draft, lambda value: value['resources']['db'].update(credentials={'password': 'PRIVATE_SENTINEL'}))
    token = validate_draft(store, 'production')['confirmation_token']
    original = Path(draft['path']).read_bytes()
    vault = Vault()
    with store.transaction():
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            apply_draft(store, 'production', token, credentials=Credentials(backend=vault))
    assert vault.writes == []
    assert Path(draft['path']).read_bytes() == original


def test_reopening_legacy_draft_adds_registered_credential_slots_and_retains_values(store):
    draft = database_draft(store)
    edit(draft, lambda value: value['resources']['db'].pop('credentials', None))
    create_draft(store, 'production')
    value = read_yaml(draft['path'])
    assert value['resources']['db']['credentials'] == {'password': None}
    assert value['resources']['db']['inputs']['host'] == 'db.example.test'


@pytest.mark.parametrize('section', ['resource', 'embedding'])
def test_credentials_sections_require_objects(store, section):
    draft = database_draft(store)
    if section == 'resource':
        edit(draft, lambda value: value['resources']['db'].update(credentials=None))
    else:
        edit(draft, lambda value: value['embedding'].update(credentials=None))
    result = validate_draft(store, 'production')
    assert not result['valid']
    assert result['issues'][0]['field'].endswith('credentials')


def test_recreated_draft_displays_saved_credential_reference(store):
    draft = database_draft(store)
    edit(draft, lambda value: value['resources']['db'].update(
        credentials={'password': {'ref': 'credential:shared.database'}}))
    vault = Vault()
    approve(store, vault)
    Path(draft['path']).unlink()
    Path(draft['path']).with_name('draft.json').unlink()
    create_draft(store, 'production', recipes=['db-postgres'])
    assert read_yaml(draft['path'])['resources']['db']['credentials'] == {
        'password': {'ref': 'credential:shared.database'}}


def test_jira_mode_change_uses_new_generated_reference_and_reuses_custom_reference(store):
    draft = create_draft(store, 'production', recipes=['issue-jira'], embedding='keyword_only')
    edit(draft, lambda value: value['resources']['issue']['inputs'].update(
        server='https://issues.example.test', username='reader@example.test'))
    edit(draft, lambda value: value['resources']['issue'].update(credentials={'token': 'PRIVATE_API_TOKEN'}))
    vault = Vault()
    approve(store, vault)
    edit(draft, lambda value: value['resources']['issue']['inputs'].update(auth_type='personal_token'))
    profile = approve(store, vault)
    assert profile['resources'][0]['connection']['auth_env'] == {
        'JIRA_PERSONAL_TOKEN': 'credential:production.issue.jira_personal_token'}
    assert read_yaml(draft['path'])['resources']['issue']['credentials']['token'] == {
        'ref': 'credential:production.issue.jira_personal_token'}


@pytest.mark.parametrize('change', [
    lambda value: value['resources']['db'].update(credentials={'PRIVATE_UNKNOWN': 'PRIVATE_SENTINEL'}),
    lambda value: value['resources']['db']['inputs'].update(password='PRIVATE_SENTINEL'),
    lambda value: value.update(credentials={'password': 'PRIVATE_SENTINEL'}),
])
def test_plaintext_is_accepted_only_in_registered_credential_fields(store, change):
    draft = database_draft(store)
    edit(draft, change)
    result = validate_draft(store, 'production')
    assert not result['valid']
    assert 'PRIVATE_' not in json.dumps(result)


def test_secret_assignments_in_comments_remain_invalid(store):
    draft = database_draft(store)
    path = Path(draft['path'])
    path.write_text('# password: PRIVATE_COMMENT\n' + path.read_text(encoding='utf-8'), encoding='utf-8')
    result = validate_draft(store, 'production')
    assert not result['valid'] and 'PRIVATE_COMMENT' not in json.dumps(result)
