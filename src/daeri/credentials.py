"""Resolve explicit credential references through supported native OS stores."""

from contextlib import contextmanager
import hashlib
import json
import os
import re
import sys

from .storage import StoreError


def resource_reference(profile, instance, field):
    name = profile + '.' + instance + '.' + field.lower()
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', name):
        identity = json.dumps([profile, instance, field.lower()], separators=(',', ':')).encode()
        name = 'resource.' + hashlib.sha256(identity).hexdigest()
    return 'credential:' + name


class Credentials:
    def __init__(self, backend=None):
        self.backend = backend

    def _native(self):
        if self.backend is None:
            try:
                if sys.platform == "darwin":
                    from keyring.backends.macOS import Keyring
                    self.backend = Keyring()
                elif sys.platform == "win32":
                    from keyring.backends.Windows import WinVaultKeyring
                    self.backend = WinVaultKeyring()
                else:
                    raise StoreError("AUTHENTICATION_REQUIRED", "A supported native credential store is required.")
            except Exception:
                raise StoreError("AUTHENTICATION_REQUIRED", "The native credential store is unavailable or locked.") from None
        return self.backend

    def _name(self, ref):
        if not isinstance(ref, str) or not re.fullmatch(r"credential:[A-Za-z0-9._-]{1,128}", ref):
            raise StoreError("INVALID_INPUT", "Use a registered credential reference.")
        return ref.removeprefix("credential:")

    def set(self, ref, value):
        name = self._name(ref)
        if not isinstance(value, str) or not value:
            raise StoreError("INVALID_INPUT", "A nonempty credential is required.")
        try:
            self._native().set_password("DAERI", name, value)
        except Exception:
            raise StoreError("AUTHENTICATION_REQUIRED", "The credential could not be stored securely.") from None

    def get(self, ref):
        if isinstance(ref, str) and re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", ref):
            value = os.environ.get(ref.removeprefix("env:"))
        else:
            value = self.read_optional(ref)
        if not value:
            raise StoreError("AUTHENTICATION_REQUIRED", "The configured credential is unavailable.")
        return value

    def read_optional(self, ref):
        name = self._name(ref)
        try:
            return self._native().get_password('DAERI', name)
        except Exception:
            raise StoreError('AUTHENTICATION_REQUIRED', 'The credential store is unavailable or locked.') from None

    @contextmanager
    def updating(self, updates):
        previous = {ref: self.read_optional(ref) for ref in updates}
        changed = {ref for ref, value in updates.items() if value != previous[ref]}
        attempted = []
        try:
            for ref in updates:
                if ref in changed:
                    attempted.append(ref)
                    self.set(ref, updates[ref])
            yield changed
        except BaseException:
            restored = True
            for ref in reversed(attempted):
                try:
                    if previous[ref] is None:
                        self.delete_if_present(ref)
                    else:
                        self.set(ref, previous[ref])
                except Exception:
                    restored = False
            if not restored:
                raise StoreError('CREDENTIAL_UPDATE_PENDING',
                                 'Credential updates require another confirmed apply.') from None
            raise

    def delete(self, ref):
        name = self._name(ref)
        try:
            self._native().delete_password("DAERI", name)
        except Exception:
            raise StoreError("AUTHENTICATION_REQUIRED", "The credential could not be removed.") from None

    def delete_if_present(self, ref):
        name = self._name(ref)
        try:
            backend = self._native()
            if backend.get_password('DAERI', name) is None:
                return False
            backend.delete_password('DAERI', name)
            return True
        except Exception:
            raise StoreError('AUTHENTICATION_REQUIRED', 'The credential store is unavailable or locked.') from None
