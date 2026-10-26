from datetime import datetime, timezone
import configparser
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from daeri.infrastructure.provider_login import ProviderLoginError, device_login


class ApiError(Exception):
    def __init__(self, code):
        self.response = {'Error': {'Code': code, 'Message': 'PRIVATE_UPSTREAM_SENTINEL'}}


class Client:
    def __init__(self, outcomes=None):
        self.outcomes = iter(outcomes or [{'accessToken': 'ACCESS_PRIVATE_SENTINEL', 'refreshToken': 'REFRESH_PRIVATE_SENTINEL', 'expiresIn': 3600}])
        self.requests = []
    def register_client(self, **kwargs):
        self.requests.append(kwargs)
        return {'clientId': 'public-client', 'clientSecret': 'CLIENT_PRIVATE_SENTINEL', 'clientSecretExpiresAt': 9999999999}
    def start_device_authorization(self, **kwargs):
        self.requests.append(kwargs)
        return {'deviceCode': 'DEVICE_PRIVATE_SENTINEL', 'userCode': 'ABCD-EFGH',
                'verificationUri': 'https://device.sso.ap-northeast-2.amazonaws.com/',
                'verificationUriComplete': 'https://device.sso.ap-northeast-2.amazonaws.com/?user_code=ABCD-EFGH',
                'expiresIn': 600, 'interval': 2}
    def create_token(self, **kwargs):
        self.requests.append(kwargs)
        result = next(self.outcomes)
        if isinstance(result, Exception): raise result
        return result


def settings(**changes):
    return {'profile': 'operations', 'account': '111111111111', 'region': 'ap-northeast-2',
            'start_url': 'https://example.awsapps.com/start', 'sso_region': 'ap-northeast-2',
            'role': 'Reader', 'no_browser': True, **changes}


def test_device_authentication_publishes_standard_private_cache_without_exposing_tokens(tmp_path):
    events = []
    result = device_login(settings(), client=Client(), home=tmp_path, emit=events.append,
                          sleep=lambda _: None, monotonic=lambda: 0)
    assert result['authenticated'] is True
    assert events[0]['user_code'] == 'ABCD-EFGH'
    public = json.dumps([events, result])
    assert 'PRIVATE_SENTINEL' not in public
    config = (tmp_path / '.aws/config').read_text()
    assert 'sso_account_id = 111111111111' in config
    assert 'sso_role_name = Reader' in config
    session = 'daeri-operations'
    path = tmp_path / '.aws/sso/cache' / (hashlib.sha1(session.encode()).hexdigest() + '.json')
    cached = json.loads(path.read_text())
    assert cached['accessToken'] == 'ACCESS_PRIVATE_SENTINEL'
    assert cached['refreshToken'] == 'REFRESH_PRIVATE_SENTINEL'
    assert cached['clientSecret'] == 'CLIENT_PRIVATE_SENTINEL'
    if os.name != 'nt': assert path.stat().st_mode & 0o077 == 0


def test_pending_and_slow_down_respect_bounded_polling(tmp_path):
    client = Client([ApiError('AuthorizationPendingException'), ApiError('SlowDownException'),
                     {'accessToken': 'ACCESS_PRIVATE_SENTINEL', 'expiresIn': 600}])
    sleeps = []
    result = device_login(settings(), client=client, home=tmp_path, emit=lambda _: None,
                          sleep=sleeps.append, monotonic=lambda: 0)
    assert result['authenticated'] is True
    assert sleeps == [2, 2, 7]


@pytest.mark.parametrize('code', ['AccessDeniedException', 'ExpiredTokenException', 'InvalidClientException'])
def test_provider_denial_never_saves_a_token_or_echoes_server_body(tmp_path, code):
    with pytest.raises(ProviderLoginError) as caught:
        device_login(settings(), client=Client([ApiError(code)]), home=tmp_path,
                     emit=lambda _: None, sleep=lambda _: None, monotonic=lambda: 0)
    assert 'PRIVATE_UPSTREAM_SENTINEL' not in str(caught.value)
    assert not (tmp_path / '.aws/config').exists()
    assert not list((tmp_path / '.aws').glob('sso/cache/*.json'))


def test_login_deadline_stops_without_requesting_new_grants(tmp_path):
    ticks = iter([0, 700])
    with pytest.raises(ProviderLoginError):
        device_login(settings(), client=Client(), home=tmp_path, emit=lambda _: None,
                     sleep=lambda _: None, monotonic=lambda: next(ticks))
    assert not (tmp_path / '.aws/config').exists()


def test_existing_provider_profile_is_preserved_and_reused(tmp_path):
    aws = tmp_path / '.aws'
    aws.mkdir()
    (aws / 'config').write_text('[profile unrelated]\nregion = eu-west-1\n[profile operations]\nsso_start_url = https://example.awsapps.com/start\nsso_region = ap-northeast-2\nsso_account_id = 111111111111\nsso_role_name = Reader\nregion = ap-northeast-2\n')
    result = device_login(settings(start_url=None, sso_region=None, role=None), client=Client(), home=tmp_path,
                          emit=lambda _: None, sleep=lambda _: None, monotonic=lambda: 0)
    assert result['authenticated'] is True
    assert '[profile unrelated]' in (aws / 'config').read_text()
    key = hashlib.sha1(b'https://example.awsapps.com/start').hexdigest()
    assert (aws / f'sso/cache/{key}.json').exists()


def test_different_account_profile_is_not_overwritten(tmp_path):
    aws = tmp_path / '.aws'
    aws.mkdir()
    path = aws / 'config'
    content = '[profile operations]\nsso_account_id = 222222222222\n'
    path.write_text(content)
    with pytest.raises(ProviderLoginError):
        device_login(settings(), client=Client(), home=tmp_path, emit=lambda _: None)
    assert path.read_text() == content


@pytest.mark.parametrize('session_settings', [
    'sso_start_url = https://other.awsapps.com/start\nsso_region = ap-northeast-2\n',
    'sso_start_url = https://example.awsapps.com/start\nsso_region = eu-west-1\n',
])
def test_new_provider_profile_preserves_an_existing_session_with_different_context(tmp_path, session_settings):
    aws = tmp_path / '.aws'
    aws.mkdir()
    path = aws / 'config'
    original = ('[profile unrelated]\nsso_session = daeri-operations\n'
                '[sso-session daeri-operations]\n' + session_settings)
    path.write_text(original)
    client = Client()
    with pytest.raises(ProviderLoginError):
        device_login(settings(), client=client, home=tmp_path, emit=lambda _: None,
                     sleep=lambda _: None, monotonic=lambda: 0)
    assert client.requests == []
    assert path.read_text() == original
    assert not list(aws.glob('sso/cache/*.json'))


def test_new_provider_profile_reuses_an_existing_matching_session(tmp_path):
    aws = tmp_path / '.aws'
    aws.mkdir()
    path = aws / 'config'
    path.write_text('[profile unrelated]\nsso_session = daeri-operations\n'
                    '[sso-session daeri-operations]\n'
                    'sso_start_url = https://example.awsapps.com/start\nsso_region = ap-northeast-2\n')
    result = device_login(settings(start_url=None, sso_region=None), client=Client(), home=tmp_path,
                          emit=lambda _: None, sleep=lambda _: None, monotonic=lambda: 0)
    assert result['authenticated'] is True
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    assert parser['profile unrelated']['sso_session'] == 'daeri-operations'
    assert parser['profile operations']['sso_session'] == 'daeri-operations'
    assert parser['sso-session daeri-operations']['sso_start_url'] == 'https://example.awsapps.com/start'
    assert parser['sso-session daeri-operations']['sso_region'] == 'ap-northeast-2'


def test_expiry_during_poll_delay_prevents_late_token_request(tmp_path):
    clock = [0]
    client = Client()
    def sleep(_): clock[0] = 700
    with pytest.raises(ProviderLoginError):
        device_login(settings(), client=client, home=tmp_path, emit=lambda _: None,
                     sleep=sleep, monotonic=lambda: clock[0])
    assert len(client.requests) == 2


@pytest.mark.parametrize('profile', ['operations reader@example.test', "operations 'reader'@example.test"])
def test_provider_profile_names_survive_login_reuse_and_target_binding(tmp_path, monkeypatch, profile):
    from daeri.infrastructure.adapters import get_adapter
    result = device_login(settings(profile=profile), client=Client(), home=tmp_path,
                          emit=lambda _: None, sleep=lambda _: None, monotonic=lambda: 0)
    assert result['provider_profile'] == profile
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(tmp_path / '.aws/config')
    sections = [shlex.split(section) for section in parser.sections()]
    assert ['profile', profile] in sections
    assert ['sso-session', 'daeri-' + profile] in sections
    before = (tmp_path / '.aws/config').read_text()
    device_login(settings(profile=profile, start_url=None, sso_region=None, role=None),
                 client=Client(), home=tmp_path, emit=lambda _: None,
                 sleep=lambda _: None, monotonic=lambda: 0)
    assert (tmp_path / '.aws/config').read_text() == before
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    resource = {'target': {'profile': profile, 'account': '111111111111', 'region': 'ap-northeast-2'},
                'connection': {'transport': 'stdio', 'env': {'AWS_PROFILE': profile, 'AWS_REGION': 'ap-northeast-2'}}}
    get_adapter('aws_read').binding_files(resource, {})


@pytest.mark.parametrize('profile', ['operations[reader', 'operations]reader', 'operations\nreader',
                                    'operations\rreader', 'operations\x00reader', 'operations\n', '', 'a' * 129])
def test_provider_profile_name_rejection_never_starts_login_or_writes_configuration(tmp_path, profile):
    client = Client()
    with pytest.raises(ProviderLoginError):
        device_login(settings(profile=profile), client=client, home=tmp_path, emit=lambda _: None)
    assert client.requests == []
    assert not (tmp_path / '.aws').exists()


def test_provider_login_remains_a_standalone_isolated_script(tmp_path):
    import daeri.infrastructure.provider_login as provider_login
    result = subprocess.run([sys.executable, '-I', provider_login.__file__],
                            input=json.dumps(settings(profile='invalid[profile')),
                            text=True, capture_output=True, timeout=10, cwd=tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout)['error']['code'] == 'AUTHENTICATION_REQUIRED'
    assert result.stderr == ''


@pytest.mark.parametrize('source', ['environment', 'service_environment', 'configuration'])
def test_provider_client_uses_the_region_endpoint_despite_configured_overrides(tmp_path, monkeypatch, source):
    pytest.importorskip('botocore', reason='Provider SDK is installed only in the Infra recipe environment.')
    from daeri.infrastructure.provider_login import _client
    path = tmp_path / 'config'
    path.write_text('[default]\n' + ('endpoint_url = https://alternate.example.test\n'
                                     if source == 'configuration' else ''))
    monkeypatch.setenv('AWS_CONFIG_FILE', str(path))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'credentials'))
    monkeypatch.setenv('AWS_PROFILE', 'default')
    monkeypatch.setenv('AWS_EC2_METADATA_DISABLED', 'true')
    for field in ('AWS_ENDPOINT_URL', 'AWS_ENDPOINT_URL_SSO_OIDC'):
        monkeypatch.delenv(field, raising=False)
    if source != 'configuration':
        field = 'AWS_ENDPOINT_URL' if source == 'environment' else 'AWS_ENDPOINT_URL_SSO_OIDC'
        monkeypatch.setenv(field, 'https://alternate.example.test')
    client = _client('ap-northeast-2')
    try:
        assert client.meta.endpoint_url == 'https://oidc.ap-northeast-2.amazonaws.com'
    finally:
        client.close()


def test_existing_quoted_provider_profile_and_sso_session_are_reused(tmp_path):
    aws = tmp_path / '.aws'
    aws.mkdir()
    path = aws / 'config'
    path.write_text('[profile "operations reader@example.test"]\nsso_session = existing reader\n'
                    'sso_account_id = 111111111111\nsso_role_name = Reader\nregion = ap-northeast-2\n'
                    '[sso-session "existing reader"]\nsso_start_url = https://example.awsapps.com/start\n'
                    'sso_region = ap-northeast-2\n')
    result = device_login(settings(profile='operations reader@example.test', start_url=None,
                                   sso_region=None, role=None), client=Client(), home=tmp_path,
                          emit=lambda _: None, sleep=lambda _: None, monotonic=lambda: 0)
    assert result['authenticated'] is True
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path)
    assert parser.sections() == ['profile "operations reader@example.test"', 'sso-session "existing reader"']
    assert (aws / 'sso/cache' / (hashlib.sha1(b'existing reader').hexdigest() + '.json')).is_file()


AMBIGUOUS_PROFILES = [
    ('operations', 'profile operations', 'profile "operations"'),
    ('default', 'default', 'profile default'),
    ('default', 'default', 'profile "default"'),
    ('operations', 'profile operations', 'profileX operations'),
]


@pytest.mark.parametrize('profile,first,second', AMBIGUOUS_PROFILES)
def test_ambiguous_provider_profile_sections_are_not_overwritten(tmp_path, profile, first, second):
    from daeri.infrastructure.provider_login import _configuration
    aws = tmp_path / '.aws'
    aws.mkdir()
    path = aws / 'config'
    original = f'[{first}]\nsso_account_id = 111111111111\n[{second}]\nsso_account_id = 222222222222\n'
    path.write_text(original)
    with pytest.raises((ProviderLoginError, ValueError)):
        _configuration(settings(profile=profile), tmp_path)
    assert path.read_text() == original


@pytest.mark.parametrize('profile,first,second', AMBIGUOUS_PROFILES)
def test_infra_binding_rejects_ambiguous_provider_profiles(tmp_path, monkeypatch, profile, first, second):
    from daeri.infrastructure.adapters import get_adapter
    from daeri.core.errors import StoreError
    aws = tmp_path / '.aws'
    aws.mkdir()
    (aws / 'config').write_text(f'[{first}]\nsso_account_id = 111111111111\n'
                              f'[{second}]\nsso_account_id = 222222222222\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    resource = {'target': {'profile': profile, 'account': '111111111111', 'region': 'ap-northeast-2'},
                'connection': {'transport': 'stdio', 'env': {'AWS_PROFILE': profile, 'AWS_REGION': 'ap-northeast-2'}}}
    with pytest.raises(StoreError) as caught:
        get_adapter('aws_read').binding_files(resource, {})
    assert caught.value.code == 'UNSUPPORTED_CONSTRAINT'


def test_infra_binding_checks_default_source_profile_aliases(tmp_path, monkeypatch):
    from daeri.infrastructure.adapters import get_adapter
    from daeri.core.errors import StoreError
    aws = tmp_path / '.aws'
    aws.mkdir()
    (aws / 'config').write_text('[profile operations]\nrole_arn = arn:aws:iam::111111111111:role/reader\n'
                               'source_profile = default\n[default]\nregion = ap-northeast-2\n'
                               '[profile default]\ncredential_process = /unapproved/helper\n')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    resource = {'target': {'profile': 'operations', 'account': '111111111111', 'region': 'ap-northeast-2'},
                'connection': {'transport': 'stdio', 'env': {'AWS_PROFILE': 'operations', 'AWS_REGION': 'ap-northeast-2'}}}
    with pytest.raises(StoreError) as caught:
        get_adapter('aws_read').binding_files(resource, {})
    assert caught.value.code == 'UNSUPPORTED_CONSTRAINT'


def test_provider_login_rejects_ambiguous_sdk_sso_session_prefixes(tmp_path):
    from daeri.infrastructure.provider_login import _configuration
    aws = tmp_path / '.aws'
    aws.mkdir()
    original = ('[profile operations]\nsso_session = operations\nsso_account_id = 111111111111\n'
                '[sso-session operations]\nsso_start_url = https://example.awsapps.com/start\nsso_region = ap-northeast-2\n'
                '[sso-sessionX operations]\nsso_start_url = https://other.awsapps.com/start\nsso_region = ap-northeast-2\n')
    (aws / 'config').write_text(original)
    with pytest.raises((ProviderLoginError, ValueError)):
        _configuration(settings(), tmp_path)
    assert (aws / 'config').read_text() == original


def test_unambiguous_default_provider_profile_can_login_and_bind(tmp_path, monkeypatch):
    from daeri.infrastructure.adapters import get_adapter
    result = device_login(settings(profile='default'), client=Client(), home=tmp_path,
                          emit=lambda _: None, sleep=lambda _: None, monotonic=lambda: 0)
    assert result['provider_profile'] == 'default'
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(tmp_path / '.aws/config')
    assert parser.sections() == ['default', 'sso-session daeri-default']
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    resource = {'target': {'profile': 'default', 'account': '111111111111', 'region': 'ap-northeast-2'},
                'connection': {'transport': 'stdio', 'env': {'AWS_PROFILE': 'default', 'AWS_REGION': 'ap-northeast-2'}}}
    get_adapter('aws_read').binding_files(resource, {})
