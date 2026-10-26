import builtins
import struct

import pytest

import daeri.infrastructure.memory as memory
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError
from test_memory import configured_resource, memory_policy, save, source


@pytest.fixture
def populated(tmp_path):
    store = Store(tmp_path)
    alpha = store.put_profile("alpha")
    beta = store.put_profile("beta")
    memory.MemoryService(store, auto_load=False)
    with store.transaction():
        for identity, profile, current, invalidated in (
            ("alpha-memory", alpha, "alpha-current", 0),
            ("alpha-invalid", alpha, "alpha-invalidated", 1),
            ("beta-memory", beta, "beta-current", 0),
        ):
            store._db.execute("INSERT INTO memory_heads VALUES(?,?,?,?)",
                              (identity, profile["profile_id"], current, invalidated))
        for reference, identity, profile, version in (
            ("alpha-old", "alpha-memory", alpha, 1),
            ("alpha-current", "alpha-memory", alpha, 2),
            ("alpha-invalidated", "alpha-invalid", alpha, 1),
            ("beta-current", "beta-memory", beta, 1),
        ):
            store._db.execute("""INSERT INTO memory_versions(memory_ref,memory_id,profile_id,version,body,
                content_hash,source_hash,created_at,expires_at,index_state) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                              (reference, identity, profile["profile_id"], version, "{}", "content", "source",
                               "2020-01-01T00:00:00+00:00", "2021-01-01T00:00:00+00:00", "ready"))
            store._db.execute("INSERT INTO memory_fts VALUES(?,?)", (reference, "shared-search-term"))
            store._db.execute("INSERT INTO memory_vectors VALUES(?,?,?,?)",
                              (reference, "fixture", 3, struct.pack("<3f", 1.0, 0.0, 0.0)))
    yield store, alpha, beta
    store.close()


def test_purge_removes_all_profile_revisions_and_indexes_without_loading_a_model(populated, monkeypatch):
    store, alpha, beta = populated
    model_dir = store.data_dir / "models"
    model_dir.mkdir()
    marker = model_dir / "shared-model"
    marker.write_bytes(b"shared installation")
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"fastembed", "sqlite_vec"}:
            raise AssertionError("Profile deletion must not load model or vector runtimes.")
        return original_import(name, *args, **kwargs)

    def forbidden_profile_lookup(*args, **kwargs):
        raise AssertionError("Deletion must work after the profile access fence is set.")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(store, "get_profile", forbidden_profile_lookup)
    assert memory.purge_profile_memory(store, alpha["profile_id"]) == 3
    assert [row[0] for row in store._db.execute("SELECT profile_id FROM memory_heads")] == [beta["profile_id"]]
    assert [row[0] for row in store._db.execute("SELECT memory_ref FROM memory_versions")] == ["beta-current"]
    assert [row[0] for row in store._db.execute("SELECT memory_ref FROM memory_vectors")] == ["beta-current"]
    assert [row[0] for row in store._db.execute("SELECT memory_ref FROM memory_fts WHERE memory_fts MATCH ?",
                                               ('"shared-search-term"',))] == ["beta-current"]
    assert marker.read_bytes() == b"shared installation"
    assert memory.purge_profile_memory(store, alpha["profile_id"]) == 0


def test_purge_without_memory_tables_does_not_initialize_memory_storage(tmp_path):
    store = Store(tmp_path)
    try:
        profile = store.put_profile("empty")
        assert memory.purge_profile_memory(store, profile["profile_id"]) == 0
        assert store._db.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'memory_%'").fetchall() == []
    finally:
        store.close()


def test_purge_rejects_incomplete_memory_schema_without_changing_it(populated):
    store, alpha, _ = populated
    with store.transaction():
        store._db.execute("DROP TABLE memory_vectors")
    with pytest.raises(StoreError) as error:
        memory.purge_profile_memory(store, alpha["profile_id"])
    assert error.value.code == "STORE_UNAVAILABLE"
    assert store._db.execute("SELECT count(*) FROM memory_versions").fetchone()[0] == 4
    assert store._db.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 4


def test_purge_joins_profile_deletion_transaction_and_rolls_back_on_later_failure(populated):
    store, alpha, _ = populated
    with pytest.raises(RuntimeError, match="later cleanup failed"):
        with store.transaction():
            assert memory.purge_profile_memory(store, alpha["profile_id"]) == 3
            raise RuntimeError("later cleanup failed")
    assert store._db.execute("SELECT count(*) FROM memory_heads").fetchone()[0] == 3
    assert store._db.execute("SELECT count(*) FROM memory_versions").fetchone()[0] == 4
    assert store._db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 4
    assert store._db.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 4


@pytest.fixture
def usable_memories(tmp_path):
    store = Store(tmp_path)
    alpha = store.put_profile("alpha", resources=[configured_resource()], memory_policy=memory_policy())
    beta = store.put_profile("beta", resources=[configured_resource()], memory_policy=memory_policy())
    service = memory.MemoryService(store, auto_load=False)
    alpha_item = save(service, alpha, sources=[source(service, alpha, key="alpha")])
    beta_item = save(service, beta, sources=[source(service, beta, key="beta")])
    yield service, alpha, alpha_item, beta, beta_item
    store.close()


@pytest.mark.parametrize("state", ["deleting", "deleted"])
def test_memory_restart_and_cleanup_preserve_pending_cascade_and_other_profile_access(usable_memories, state):
    original, alpha, alpha_item, beta, beta_item = usable_memories
    with original.store.transaction():
        original.store._db.execute("INSERT INTO profile_deletions VALUES(?,?,NULL)", (alpha["profile_id"], state))
    reopened = Store(original.store.data_dir)
    try:
        service = memory.MemoryService(reopened, auto_load=False)
        assert service.cleanup() == {"deleted": 0, "pending": 0}
        assert [item["memory_ref"] for item in service.search(beta["profile_id"], "E_CONN_001")["items"]] == [beta_item["memory_ref"]]
        assert reopened._db.execute("SELECT body FROM memory_versions WHERE memory_ref=?",
                                    (alpha_item["memory_ref"],)).fetchone()[0] is not None
        with pytest.raises(StoreError) as error:
            service.get(alpha["profile_id"], alpha_item["memory_ref"])
        assert error.value.code == ("PROFILE_DELETING" if state == "deleting" else "PROFILE_DELETED")
    finally:
        reopened.close()


@pytest.mark.parametrize("state", ["deleting", "deleted"])
def test_global_reindex_only_rebuilds_available_profile_memories(usable_memories, state):
    service, alpha, alpha_item, _, _ = usable_memories
    with service.store.transaction():
        service.store._db.execute("INSERT INTO profile_deletions VALUES(?,?,NULL)", (alpha["profile_id"], state))
    assert service.reindex() == {"ready": 0, "pending": 1}
    assert service.store._db.execute("SELECT body FROM memory_versions WHERE memory_ref=?",
                                     (alpha_item["memory_ref"],)).fetchone()[0] is not None
