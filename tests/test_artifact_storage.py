from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


def test_managed_artifacts_have_integrity_expiry_and_no_arbitrary_paths(tmp_path):
    store = Store(tmp_path)
    try:
        profile = store.put_profile("test")
        store.insert_task({"task_id": "task", "task_revision": 1, "profile_id": profile["profile_id"]})
        ref = store.write_artifact("task", b"name,value\r\na,1\r\n", "csv", "text/csv", "csv")
        metadata = store.artifact_file("task", ref)
        path = Path(metadata["local_path"])
        assert path.parent == tmp_path / "artifacts"
        assert path.read_bytes() == b"name,value\r\na,1\r\n"
        with pytest.raises(StoreError):
            store.write_artifact("task", b"x", "text", "text/plain", "../bad")
        path.write_bytes(b"changed")
        with pytest.raises(StoreError, match="RECORD_UNAVAILABLE"):
            store.artifact_file("task", ref)
        store.set_task_expiry("task", (datetime.now(UTC)-timedelta(seconds=1)).isoformat())
        with pytest.raises(StoreError, match="RECORD_EXPIRED"):
            store.artifact_file("task", ref)
        store.cleanup()
        assert not path.exists()
    finally:
        store.close()


def test_artifact_with_rolled_back_reference_is_cleaned_on_next_start(tmp_path):
    store = Store(tmp_path)
    profile = store.put_profile("test")
    store.insert_task({"task_id": "task", "task_revision": 1, "profile_id": profile["profile_id"]})
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.write_artifact("task", b"unpublished", "text", "text/plain", "txt")
            raise RuntimeError("interrupted")
    store.close()
    reopened = Store(tmp_path)
    try:
        assert list((tmp_path / "artifacts").iterdir()) == []
    finally:
        reopened.close()


def test_artifact_without_original_database_is_preserved_for_recovery(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    path = artifacts / ("a" * 32 + ".csv")
    path.write_bytes(b"original-file\n")
    with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
        Store(tmp_path)
    assert path.read_bytes() == b"original-file\n"
