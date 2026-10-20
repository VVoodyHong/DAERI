"""Explicit device authorization in the prepared provider SDK, separate from task approval."""

import configparser
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4
import webbrowser


# A strict end assertion also works with JSON Schema's pattern search semantics.
PROFILE_NAME_PATTERN = r'^[^\[\]\x00-\x1f\x7f]{1,128}(?![\s\S])'


def provider_section(parser, kind, name):
    matches = []
    for section in parser.sections():
        if kind == 'profile' and name == 'default' and section == 'default':
            matches.append(section)
            continue
        # The provider SDK recognizes section kinds by prefix, not exact token.
        if not section.startswith(kind):
            continue
        try:
            parts = shlex.split(section)
        except ValueError:
            continue
        if len(parts) == 2 and parts[1] == name:
            matches.append(section)
    if len(matches) > 1:
        raise ValueError('Provider section names must be unambiguous.')
    if matches:
        return matches[0]
    return 'default' if kind == 'profile' and name == 'default' else kind + ' ' + shlex.quote(name)


class ProviderLoginError(Exception):
    pass


def _require(value, message='Provider login settings or authorization are invalid.'):
    if not value:
        raise ProviderLoginError(message)


def _private(path, content):
    _require(not path.is_symlink(), 'Provider storage cannot use a symbolic link.')
    temporary = path.with_name('.daeri-' + uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _directory(path):
    _require(not path.is_symlink(), 'Provider storage cannot use a symbolic link.')
    path.mkdir(exist_ok=True, mode=0o700)
    _require(path.is_dir())


def _configuration(request, home):
    profile = request['profile']
    _require(isinstance(profile, str) and re.fullmatch(PROFILE_NAME_PATTERN, profile))
    _require(isinstance(request['account'], str) and re.fullmatch(r'\d{12}', request['account']))
    _require(isinstance(request['region'], str) and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d+', request['region']))
    aws = home / '.aws'
    path = aws / 'config'
    _require(not aws.is_symlink() and not path.is_symlink(), 'Provider configuration cannot use a symbolic link.')
    original = path.read_bytes() if path.exists() else None
    _require(original is None or len(original) <= 2 * 1024 * 1024)
    parser = configparser.ConfigParser(interpolation=None)
    if original: parser.read_string(original.decode('utf-8'))
    section = provider_section(parser, 'profile', profile)
    existing = dict(parser[section]) if parser.has_section(section) else {}
    _require(not existing.get('sso_account_id') or existing['sso_account_id'] == request['account'],
             'The configured provider account differs from the selected DAERI target.')
    session_name = existing.get('sso_session')
    session_section = provider_section(parser, 'sso-session', session_name) if session_name else None
    session = dict(parser[session_section]) if session_section and parser.has_section(session_section) else existing
    start_url = request.get('start_url') or session.get('sso_start_url')
    region = request.get('sso_region') or session.get('sso_region')
    role = request.get('role') or existing.get('sso_role_name')
    _require(isinstance(start_url, str) and len(start_url) <= 2048)
    parsed = urlsplit(start_url)
    _require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
             and not parsed.query and not parsed.fragment)
    _require(isinstance(region, str) and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d+', region))
    _require(isinstance(role, str) and re.fullmatch(r'[A-Za-z0-9_+=,.@-]{1,128}', role))
    for key, expected in [('sso_start_url', start_url), ('sso_region', region)]:
        _require(not session.get(key) or session[key] == expected, 'The existing provider login context differs; use a separate profile.')
    _require(not existing.get('sso_role_name') or existing['sso_role_name'] == role,
             'The existing provider role differs; use a separate profile.')
    if not parser.has_section(section):
        parser.add_section(section)
        session_name = 'daeri-' + profile
        session_section = provider_section(parser, 'sso-session', session_name)
        if not parser.has_section(session_section): parser.add_section(session_section)
        parser[section]['sso_session'] = session_name
    if session_name:
        _require(session_section and parser.has_section(session_section), 'The selected SSO session configuration is missing.')
        parser[session_section]['sso_start_url'] = start_url
        parser[session_section]['sso_region'] = region
        parser[session_section]['sso_registration_scopes'] = 'sso:account:access'
    else:
        parser[section]['sso_start_url'] = start_url
        parser[section]['sso_region'] = region
    parser[section]['sso_account_id'] = request['account']
    parser[section]['sso_role_name'] = role
    parser[section]['region'] = request['region']
    return parser, original, start_url, region, role, session_name


def _client(region):
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.session import get_session
    return get_session().create_client('sso-oidc', region_name=region,
        config=Config(signature_version=UNSIGNED, connect_timeout=10, read_timeout=30,
                      retries={'max_attempts': 0}))


def device_login(request, *, client=None, home=None, emit=None, sleep=time.sleep,
                 monotonic=time.monotonic, browser=webbrowser.open):
    home = Path.home() if home is None else Path(home)
    emit = (lambda value: print(json.dumps({'status': 'pending', 'data': value, 'error': None}), flush=True)) if emit is None else emit
    parser, original, start_url, region, role, session_name = _configuration(request, home)
    client = _client(region) if client is None else client
    try:
        registration = client.register_client(clientName='DAERI', clientType='public',
                                              scopes=['sso:account:access'])
        device = client.start_device_authorization(clientId=registration['clientId'],
                                                   clientSecret=registration['clientSecret'], startUrl=start_url)
        uri = device.get('verificationUriComplete') or device['verificationUri']
        parsed = urlsplit(uri)
        _require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
                 and parsed.hostname.endswith(('.amazonaws.com', '.awsapps.com', '.amazonaws.com.cn', '.awsapps.cn')),
                 'The provider returned an unsupported verification URL.')
        _require(isinstance(device['userCode'], str) and re.fullmatch(r'[A-Za-z0-9-]{1,64}', device['userCode']))
        emit({'provider': 'infra_sso', 'verification_uri': uri, 'user_code': device['userCode'],
              'provider_profile': request['profile']})
        if not request.get('no_browser', False):
            try: browser(uri)
            except Exception: pass
        _require(type(device['expiresIn']) is int and 0 < device['expiresIn'] <= 3600)
        interval = max(1, min(int(device.get('interval', 5)), 30))
        deadline = monotonic() + min(device['expiresIn'], 600)
        while monotonic() < deadline:
            sleep(interval)
            if monotonic() >= deadline:
                raise ProviderLoginError('Provider authentication timed out; request a new device authorization explicitly.')
            try:
                token = client.create_token(clientId=registration['clientId'], clientSecret=registration['clientSecret'],
                                            grantType='urn:ietf:params:oauth:grant-type:device_code', deviceCode=device['deviceCode'])
                break
            except Exception as error:
                code = getattr(error, 'response', {}).get('Error', {}).get('Code')
                if code == 'AuthorizationPendingException': continue
                if code == 'SlowDownException':
                    interval = min(interval + 5, 30)
                    continue
                raise ProviderLoginError('Provider authentication was denied, expired, or unavailable.') from None
        else:
            raise ProviderLoginError('Provider authentication timed out; request a new device authorization explicitly.')
        _require(isinstance(token.get('accessToken'), str) and token['accessToken']
                 and type(token.get('expiresIn')) is int and 0 < token['expiresIn'] <= 86400)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=token['expiresIn'])).strftime('%Y-%m-%dT%H:%M:%SZ')
        cached = {'startUrl': start_url, 'region': region, 'accessToken': token['accessToken'], 'expiresAt': expires}
        if token.get('refreshToken'):
            cached.update(refreshToken=token['refreshToken'], clientId=registration['clientId'],
                          clientSecret=registration['clientSecret'],
                          registrationExpiresAt=datetime.fromtimestamp(registration['clientSecretExpiresAt'], timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        aws = home / '.aws'
        _directory(aws)
        _directory(aws / 'sso')
        _directory(aws / 'sso/cache')
        current = (aws / 'config').read_bytes() if (aws / 'config').exists() else None
        _require(current == original, 'Provider configuration changed during login; retry after reviewing it.')
        key = hashlib.sha1((session_name or start_url).encode('utf-8')).hexdigest()
        _private(aws / 'sso/cache' / (key + '.json'), json.dumps(cached))
        content = io.StringIO()
        parser.write(content)
        _private(aws / 'config', content.getvalue())
        return {'authenticated': True, 'provider_profile': request['profile'], 'account': request['account'],
                'role': role, 'expires_at': expires}
    except ProviderLoginError:
        raise
    except Exception:
        raise ProviderLoginError('Provider authentication could not be completed.') from None
    finally:
        close = getattr(client, 'close', None)
        if close: close()


def main():
    logging.disable(logging.CRITICAL)
    try:
        value = sys.stdin.read(16385)
        _require(len(value) <= 16384)
        request = json.loads(value)
        _require(isinstance(request, dict) and set(request) <= {'profile', 'account', 'region', 'start_url', 'sso_region', 'role', 'no_browser'})
        result = {'status': 'ok', 'data': device_login(request), 'error': None}
        code = 0
    except Exception:
        result = {'status': 'error', 'data': None, 'error': {'code': 'AUTHENTICATION_REQUIRED',
                  'message': 'Provider authentication could not be completed. Review the selected SSO profile and retry explicitly.', 'next_action': 'stop'}}
        code = 1
    print(json.dumps(result), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
