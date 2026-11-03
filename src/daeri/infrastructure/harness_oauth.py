"""Request-scoped user delegation through the native InvokeHarness transport."""

from contextlib import contextmanager
from contextvars import ContextVar

from botocore import UNSIGNED

from daeri.core.access import AuthenticatedUser
from daeri.core.errors import StoreError


_identity = ContextVar('daeri_harness_identity', default=None)


@contextmanager
def bind_harness_identity(user, token, audience):
    """Only the verified MCP authentication boundary supplies this ephemeral context."""
    if (not isinstance(user, AuthenticatedUser) or not isinstance(token, str)
            or not 0 < len(token) <= 65536 or any(ord(char) <= 32 or ord(char) >= 127 for char in token)
            or not isinstance(audience, str) or not audience.strip()):
        raise StoreError('AUTHENTICATION_REQUIRED', 'A verified access token is required for user delegation.')
    user.contexts()
    handle = _identity.set((user, token, audience))
    try:
        yield
    finally:
        _identity.reset(handle)


class OAuthHarnessClient:
    def __init__(self, client, *, audience):
        if client.meta.config.signature_version is not UNSIGNED:
            raise ValueError('The OAuth harness client requires an unsigned native SDK client.')
        if not isinstance(audience, str) or not audience.strip():
            raise ValueError('An explicit accepted harness token audience is required.')
        self.client, self.audience = client, audience
        client.meta.events.register('before-call.bedrock-agentcore.InvokeHarness', self._authorize)

    def _current(self):
        identity = _identity.get()
        if identity is None or identity[2] != self.audience:
            raise StoreError('AUTHENTICATION_REQUIRED', 'Sign in with an access token accepted by the project harness.')
        identity[0].contexts()
        return identity

    def _authorize(self, params, **kwargs):
        user, token, _ = self._current()
        params['headers']['Authorization'] = 'Bearer ' + token

    def invoke_harness(self, **request):
        user, _, _ = self._current()
        if request.get('actorId') != user.actor_id or request.get('runtimeUserId') != user.actor_id:
            raise StoreError('AUTHENTICATION_REQUIRED', 'The harness request must use the authenticated identity.')
        return self.client.invoke_harness(**request)
