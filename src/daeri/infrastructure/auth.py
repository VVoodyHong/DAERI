"""Verify managed OIDC access tokens; never accept identity from model arguments."""

import asyncio
import re
from threading import Lock
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

from daeri.core.access import AuthenticatedUser
from daeri.core.endpoints import mcp_endpoint
from daeri.core.errors import StoreError


_SCOPE = re.compile(r'[\x21\x23-\x5b\x5d-\x7e]+')


def https_url(value: str) -> str:
    """Validate configured/discovered HTTPS endpoints without making requests."""
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError('A trusted HTTPS URL is required.')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('A trusted HTTPS URL without credentials, query or fragment is required.')
    parsed.port
    return value


class ProjectAccessToken(AccessToken):
    user: AuthenticatedUser


class JwtTokenVerifier:
    def __init__(self, issuer: str, audience: str, resource_url: str, *, project_claim='projects'):
        self.issuer, self.resource_url = https_url(issuer), mcp_endpoint(resource_url)
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError('An explicit resource token audience is required.')
        if not isinstance(project_claim, str) or not project_claim.strip():
            raise ValueError('A project grant claim is required.')
        self.audience, self.project_claim = audience, project_claim
        self._jwks, self._lock = None, Lock()

    def _keys(self):
        with self._lock:
            if self._jwks is None:
                response = httpx.get(self.issuer.rstrip('/') + '/.well-known/openid-configuration',
                                     timeout=10, follow_redirects=False, trust_env=False)
                response.raise_for_status()
                metadata = response.json()
                if not isinstance(metadata, dict) or metadata.get('issuer') != self.issuer:
                    raise ValueError('OIDC discovery does not match the configured issuer.')
                uri = https_url(metadata.get('jwks_uri'))
                self._jwks = jwt.PyJWKClient(uri, timeout=10, lifespan=300)
            return self._jwks

    def _verify(self, token):
        try:
            if not isinstance(token, str) or not token or len(token) > 65536:
                return None
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str) or not header['kid']:
                return None
            key = self._keys().get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=['RS256'], issuer=self.issuer, audience=self.audience,
                                options={'require': ['iss', 'aud', 'sub', 'exp', 'scope', self.project_claim]})
            if (type(claims['exp']) is not int
                    or ('iat' in claims and type(claims['iat']) is not int)
                    or claims.get('token_use', 'access') != 'access'):
                return None
            scope, projects = claims['scope'], claims[self.project_claim]
            if (not isinstance(scope, str) or any(ord(char) < 32 or ord(char) > 126 for char in scope)
                    or any(not _SCOPE.fullmatch(item) for item in scope.split())
                    or not isinstance(projects, list) or any(not isinstance(item, str) for item in projects)
                    or len(set(projects)) != len(projects)):
                return None
            user = AuthenticatedUser(claims['sub'], frozenset(projects), claims['exp'])
            user.contexts()
            client_id = claims.get('client_id', claims.get('azp', self.audience))
            if not isinstance(client_id, str) or not client_id.strip():
                return None
            return ProjectAccessToken(token=token, client_id=client_id, scopes=scope.split(),
                                      expires_at=claims['exp'], resource=self.resource_url,
                                      subject=claims['sub'], claims={'iss': self.issuer, 'aud': self.audience}, user=user)
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, TypeError, KeyError, OSError, StoreError):
            return None

    async def verify_token(self, token: str) -> ProjectAccessToken | None:
        # Discovery and the standard JWKS cache may perform synchronous network I/O.
        return await asyncio.to_thread(self._verify, token)
