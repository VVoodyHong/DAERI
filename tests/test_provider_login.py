from datetime import datetime, timezone
import hashlib
import json
import os

import pytest

from daeri.provider_login import ProviderLoginError, device_login


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


def test_expiry_during_poll_delay_prevents_late_token_request(tmp_path):
    clock = [0]
    client = Client()
    def sleep(_): clock[0] = 700
    with pytest.raises(ProviderLoginError):
        device_login(settings(), client=client, home=tmp_path, emit=lambda _: None,
                     sleep=sleep, monotonic=lambda: clock[0])
    assert len(client.requests) == 2
