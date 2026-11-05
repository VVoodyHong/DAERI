"""Real JWT signatures and trusted issuer discovery at the remote access boundary."""

import asyncio
from dataclasses import FrozenInstanceError
from io import StringIO
import json
import time

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from daeri.core.cloud import AccessContext
from daeri.core.errors import StoreError


ISSUER = 'https://login.example.com/company'
RESOURCE = 'https://daeri.example.com/mcp'
AUDIENCE = 'https://daeri.example.com/mcp'


@pytest.fixture
def issuer(monkeypatch):
    keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]
    public = [dict(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), kid=f'key-{i}',
                   use='sig', alg='RS256') for i, key in enumerate(keys)]
    state = {'keys': [public[0]], 'discovery': {'issuer': ISSUER, 'jwks_uri': ISSUER + '/keys'},
             'requests': []}

    def discovery(url, **options):
        state['requests'].append(url)
        assert url == ISSUER + '/.well-known/openid-configuration'
        assert options['follow_redirects'] is False and options['trust_env'] is False
        return httpx.Response(200, json=state['discovery'], request=httpx.Request('GET', url))

    def jwks_open(opener, request, **options):
        state['requests'].append(request.full_url)
        assert request.full_url == ISSUER + '/keys'
        return StringIO(json.dumps({'keys': state['keys']}))

    monkeypatch.setattr(httpx, 'get', discovery)
    monkeypatch.setattr('urllib.request.OpenerDirector.open', jwks_open)

    def token(changes=None, *, index=0, algorithm='RS256', headers=None):
        claims = {'iss': ISSUER, 'aud': AUDIENCE, 'sub': 'alice', 'exp': int(time.time()) + 600,
                  'iat': int(time.time()), 'scope': 'daeri/use other', 'projects': ['charging', 'reporting']}
        claims.update(changes or {})
        return jwt.encode(claims, keys[index] if algorithm == 'RS256' else 'untrusted-secret' * 3,
                          algorithm=algorithm, headers={'kid': f'key-{index}', **(headers or {})})

    state.update(token=token, public=public)
    return state


def verifier(**options):
    from daeri.infrastructure.auth import JwtTokenVerifier
    return JwtTokenVerifier(issuer=ISSUER, audience=AUDIENCE, resource_url=RESOURCE, **options)


def test_verified_token_supplies_immutable_user_project_context_and_sdk_resource(issuer):
    from daeri.infrastructure.auth import ProjectAccessToken
    result = asyncio.run(verifier().verify_token(issuer['token']()))
    assert isinstance(result, ProjectAccessToken)
    assert result.user.context('charging') == AccessContext('alice', 'charging')
    assert result.user.contexts() == (AccessContext('alice', 'charging'), AccessContext('alice', 'reporting'))
    assert result.user.project_ids == frozenset({'charging', 'reporting'})
    assert result.subject == 'alice' and result.claims['iss'] == ISSUER
    assert result.resource == RESOURCE and result.expires_at == result.user.expires_at
    assert result.scopes == ['daeri/use', 'other']
    with pytest.raises(FrozenInstanceError):
        result.user.actor_id = 'mallory'
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        result.user.context('not-granted')


@pytest.mark.parametrize('changes', [
    {'iss': 'https://forged.example'}, {'aud': 'another-service'}, {'aud': None},
    {'exp': 1}, {'exp': True}, {'exp': '9999999999'}, {'exp': None},
    {'iat': 9999999999}, {'iat': '1'}, {'sub': ''}, {'sub': 3}, {'sub': None},
    {'scope': ['daeri/use']}, {'scope': None}, {'scope': 'daeri/use\nadmin'},
    {'projects': 'charging'}, {'projects': [3]}, {'projects': ['../charging']},
    {'projects': ['charging', 'charging']}, {'projects': None},
    {'token_use': 'id'},
])
def test_invalid_or_wrong_resource_claims_never_create_a_principal(issuer, changes):
    assert asyncio.run(verifier().verify_token(issuer['token'](changes))) is None


def test_hmac_and_wrong_rsa_signatures_cannot_forge_projects(issuer):
    target = verifier()
    assert asyncio.run(target.verify_token(issuer['token'](algorithm='HS256'))) is None
    forged = issuer['token']({'projects': ['admin']}, index=1, headers={'kid': 'key-0'})
    assert asyncio.run(target.verify_token(forged)) is None


def test_discovery_issuer_and_jwks_scheme_are_validated_before_key_lookup(issuer):
    for discovery in [{'issuer': 'https://forged.example', 'jwks_uri': ISSUER + '/keys'},
                      {'issuer': ISSUER, 'jwks_uri': 'http://login.example.com/keys'},
                      {'issuer': ISSUER, 'jwks_uri': 'https://user:secret@login.example.com/keys'},
                      {'issuer': ISSUER}]:
        issuer['requests'].clear()
        issuer['discovery'] = discovery
        assert asyncio.run(verifier().verify_token(issuer['token']())) is None
        assert issuer['requests'] == [ISSUER + '/.well-known/openid-configuration']


def test_discovery_and_key_sets_are_cached_and_unknown_kid_can_rotate(issuer, monkeypatch):
    target = verifier()
    assert asyncio.run(target.verify_token(issuer['token']())) is not None
    assert asyncio.run(target.verify_token(issuer['token']())) is not None
    assert issuer['requests'] == [ISSUER + '/.well-known/openid-configuration', ISSUER + '/keys']
    issuer['keys'] = [issuer['public'][1]]
    monotonic = time.monotonic
    monkeypatch.setattr('jwt.jwks_client.time.monotonic', lambda: monotonic() + 31)
    assert asyncio.run(target.verify_token(issuer['token'](index=1))) is not None
    assert issuer['requests'][-1] == ISSUER + '/keys'
    assert len(issuer['requests']) == 3


def test_unknown_key_ids_do_not_bypass_the_standard_jwks_refresh_cooldown(issuer):
    target = verifier()
    assert asyncio.run(target.verify_token(issuer['token']())) is not None
    assert asyncio.run(target.verify_token(issuer['token'](index=1))) is None
    assert asyncio.run(target.verify_token(issuer['token'](index=1))) is None
    assert issuer['requests'] == [ISSUER + '/.well-known/openid-configuration', ISSUER + '/keys']


def test_token_header_urls_never_override_trusted_issuer_discovery(issuer):
    token = issuer['token'](headers={'jku': 'https://forged.example/keys', 'x5u': 'https://forged.example/cert'})
    assert asyncio.run(verifier().verify_token(token)) is not None
    assert issuer['requests'] == [ISSUER + '/.well-known/openid-configuration', ISSUER + '/keys']


def test_identity_and_project_membership_are_rechecked_after_token_expiry():
    from daeri.core.access import AuthenticatedUser
    user = AuthenticatedUser('alice', frozenset({'charging'}), 1)
    with pytest.raises(StoreError, match='AUTHENTICATION_REQUIRED'):
        user.context('charging')
    with pytest.raises(StoreError, match='AUTHENTICATION_REQUIRED'):
        user.contexts()


def test_signed_project_management_grants_are_distinct_from_normal_membership(issuer):
    manager = asyncio.run(verifier().verify_token(issuer['token']({'managed_projects': ['charging']})))
    assert manager.user.manage('charging') == AccessContext('alice', 'charging')
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        manager.user.manage('reporting')
    member = asyncio.run(verifier().verify_token(issuer['token']()))
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        member.user.manage('charging')


@pytest.mark.parametrize('management', ['charging', None, [3], ['charging', 'charging'], ['not-granted']])
def test_invalid_management_grants_never_become_connection_permissions(issuer, management):
    assert asyncio.run(verifier().verify_token(issuer['token']({'managed_projects': management}))) is None


def test_identity_provider_can_configure_management_claim_name(issuer):
    token = issuer['token']({'connection_owners': ['charging']})
    result = asyncio.run(verifier(management_claim='connection_owners').verify_token(token))
    assert result.user.manage('charging').project_id == 'charging'
