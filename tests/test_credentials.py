import pytest

from daeri.credentials import Credentials
from daeri.storage import StoreError


class NativeStore:
    def __init__(self):
        self.values = {}

    def set_password(self, service, name, value):
        self.values[(service, name)] = value

    def get_password(self, service, name):
        return self.values.get((service, name))

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


def test_credentials_are_resolved_without_plaintext_files(tmp_path):
    vault = Credentials(backend=NativeStore())
    vault.set("credential:db-password", "private-test-value")
    assert vault.get("credential:db-password") == "private-test-value"
    vault.delete("credential:db-password")
    with pytest.raises(StoreError, match="AUTHENTICATION_REQUIRED"):
        vault.get("credential:db-password")
    assert list(tmp_path.iterdir()) == []


def test_only_explicit_environment_references_are_resolved(monkeypatch):
    monkeypatch.setenv("DAERI_TEST_SECRET", "explicit-test-value")
    vault = Credentials(backend=NativeStore())
    assert vault.get("env:DAERI_TEST_SECRET") == "explicit-test-value"
    for ref in ("plain-secret", "credential:../other", "env:UNCONFIGURED_TEST_VALUE"):
        with pytest.raises(StoreError):
            vault.get(ref)


def test_locked_secure_store_does_not_fall_back_to_plaintext():
    class Locked(NativeStore):
        def get_password(self, service, name):
            raise RuntimeError("PRIVATE_BACKEND_ERROR")
    vault = Credentials(backend=Locked())
    with pytest.raises(StoreError) as failure:
        vault.get("credential:test")
    assert failure.value.code == "AUTHENTICATION_REQUIRED"
    assert "PRIVATE_BACKEND_ERROR" not in str(failure.value)


def test_cascade_credential_removal_is_idempotent_and_does_not_touch_other_keys():
    backend = NativeStore()
    vault = Credentials(backend=backend)
    vault.set('credential:owned', 'owned-private')
    vault.set('credential:other', 'other-private')
    assert vault.delete_if_present('credential:owned') is True
    assert vault.delete_if_present('credential:owned') is False
    assert vault.get('credential:other') == 'other-private'


def test_cascade_does_not_treat_a_locked_vault_as_an_absent_credential():
    class Locked(NativeStore):
        def get_password(self, service, name):
            raise RuntimeError('PRIVATE_BACKEND_ERROR')
    with pytest.raises(StoreError) as error:
        Credentials(backend=Locked()).delete_if_present('credential:owned')
    assert error.value.code == 'AUTHENTICATION_REQUIRED'
    assert 'PRIVATE_BACKEND_ERROR' not in str(error.value)


def test_credential_updates_report_actual_changes_and_restore_on_caller_failure():
    backend = NativeStore()
    vault = Credentials(backend=backend)
    vault.set('credential:existing', 'old-value')
    assert vault.read_optional('credential:absent') is None
    with vault.updating({'credential:existing': 'old-value'}) as changed:
        assert changed == set()
    with pytest.raises(RuntimeError, match='CALLER_FAILED'):
        with vault.updating({'credential:existing': 'new-value', 'credential:new': 'new-secret'}) as changed:
            assert changed == {'credential:existing', 'credential:new'}
            assert vault.get('credential:existing') == 'new-value'
            raise RuntimeError('CALLER_FAILED')
    assert vault.get('credential:existing') == 'old-value'
    assert vault.read_optional('credential:new') is None


def test_credential_updates_restore_a_backend_write_that_mutates_then_fails():
    class FailsOnce(NativeStore):
        def set_password(self, service, name, value):
            super().set_password(service, name, value)
            if value == 'rejected-secret':
                raise RuntimeError('PRIVATE_BACKEND_ERROR')
    backend = FailsOnce()
    vault = Credentials(backend=backend)
    vault.set('credential:existing', 'old-value')
    with pytest.raises(StoreError) as failure:
        with vault.updating({'credential:first': 'new-value', 'credential:existing': 'rejected-secret'}):
            pytest.fail('The failed write must abort the caller.')
    assert failure.value.code == 'AUTHENTICATION_REQUIRED'
    assert backend.values == {('DAERI', 'existing'): 'old-value'}


def test_credential_update_failure_reports_incomplete_compensation_safely():
    class RestoreFails(NativeStore):
        def delete_password(self, service, name):
            raise RuntimeError('PRIVATE_BACKEND_ERROR')
    vault = Credentials(backend=RestoreFails())
    with pytest.raises(StoreError) as failure:
        with vault.updating({'credential:new': 'private-value'}):
            raise OSError('PRIVATE_FILE_ERROR')
    assert failure.value.code == 'CREDENTIAL_UPDATE_PENDING'
    assert 'PRIVATE' not in str(failure.value)


def test_empty_credential_updates_do_not_open_a_native_store(monkeypatch):
    vault = Credentials()
    monkeypatch.setattr(vault, '_native', lambda: pytest.fail('Unexpected native access'))
    with vault.updating({}) as changed:
        assert changed == set()
