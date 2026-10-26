from datetime import UTC, datetime, timedelta
import os
import socket
import hashlib
import json
import sqlite3
import sys

import pytest

from daeri.infrastructure.memory import LocalEmbedder, MemoryService
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


TARGET = {"server": "https://git.example.test", "project": "team/service"}
TARGET_HASH = hashlib.sha256(json.dumps(TARGET, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def configured_resource():
    root = {"segments": []}
    return {"instance_id": "primary-db", "type": "Source", "adapter": "source_read", "target": dict(TARGET),
        "identity_ref": "test:reader", "connection": {"transport": "stdio", "command": sys.executable, "args": ["-c", "pass"]},
        "capabilities": [{"capability_id": "read", "tool_name": "get_repository_file", "constraints": {"fixed": {"project_id": "team/service"}}}],
        "policy": {"allowed_model_fields": [root], "export_fields": [root], "retained_fields": [root], "masking": [],
            "memory": {"categories": ["service_rule", "resource_structure", "procedure", "lesson"], "source_fields": [root],
                "context_sources": ["request", "clarification"], "retention": "long_term", "allow_model_recall": True}}}


def timestamp(days=0):
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def memory_policy(**changes):
    return {"categories": ["service_rule", "resource_structure", "procedure", "lesson"],
            "instance_ids": ["primary-db"], "target_bindings": {"primary-db": TARGET_HASH},
            "model_allowed": True, "retain_allowed": True, "long_term_allowed": True,
            **changes}


@pytest.fixture
def memory(tmp_path):
    store = Store(tmp_path)
    profile = store.put_profile("alpha", resources=[configured_resource()], memory_policy=memory_policy())
    service = MemoryService(store, auto_load=False)
    yield service, profile
    store.close()


def source(service, profile, *, key="one", record_days=30, **changes):
    store = service.store
    task_id = "task-" + key
    store.insert_task({"task_id": task_id, "profile_id": profile["profile_id"],
                       "task_revision": 1, "state": "succeeded", "created_at": timestamp(),
                       "last_report_ref": "report-existing"})
    expiry = timestamp(record_days) if record_days is not None else None
    ref = store.write_record(task_id, "request", {"text": "실제 확인한 서비스 용어"}, expiry)
    return {"task_id": task_id, "record_ref": ref,
            "fields": [{"segments": [{"key": "text"}]}],
            "instance_ids": ["primary-db"], "target_bindings": {"primary-db": TARGET_HASH},
            "verified_at": timestamp(), "expires_at": expiry, "available": True,
            "model_allowed": True, "retain_allowed": True, "long_term_allowed": True, **changes}


def candidate(statement="E_CONN_001 오류는 연결 풀 포화 시 확인한다"):
    return {"category": "lesson", "statement": statement, "applicability": "운영 DB 연결 문제",
            "instance_ids": ["primary-db"], "verification_method": "사용자가 확인한 요청과 대조"}


def save(service, profile, *, draft=None, sources=None, policy=None, **kwargs):
    return service.save(profile["profile_id"], draft or candidate(),
                        source_records=sources or [source(service, profile)],
                        policy={**memory_policy(), "retention": "source_bound",
                                "retention_days": 365, "expires_at": timestamp(30), **(policy or {})},
                        **kwargs)


def test_keyword_search_persists_and_separates_profiles(memory):
    service, profile = memory
    item = save(service, profile)
    found = service.search(profile["profile_id"], "E_CONN_001")
    assert [value["memory_ref"] for value in found["items"]] == [item["memory_ref"]]
    assert found["retrieval_state"] == "keyword_only"
    assert found["limitations"]
    assert item["index_state"] == "pending"
    other = service.store.put_profile("beta", resources=[configured_resource()], memory_policy=memory_policy())
    assert service.search(other["profile_id"], "E_CONN_001")["items"] == []
    reopened = Store(service.store.data_dir)
    try:
        assert MemoryService(reopened, auto_load=False).get(profile["profile_id"], item["memory_ref"])["statement"] == candidate()["statement"]
    finally:
        reopened.close()


def test_source_bound_expiry_is_earliest_permission(memory):
    service, profile = memory
    short = source(service, profile, expires_at=timestamp(2))
    item = save(service, profile, sources=[short])
    assert datetime.fromisoformat(item["expires_at"]) <= datetime.now(UTC) + timedelta(days=2)


@pytest.mark.parametrize('days', [3_000_000, 10 ** 100])
def test_unrepresentable_approved_retention_never_publishes_memory(memory, days):
    service, profile = memory
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        save(service, profile, policy={'retention': 'long_term', 'retention_days': days})
    assert service.search(profile['profile_id'], 'E_CONN_001')['items'] == []


@pytest.mark.parametrize('missing', ['expires_at', 'body'])
def test_incomplete_memory_schema_returns_recovery_error_without_migration(tmp_path, missing):
    store = Store(tmp_path)
    columns = ['memory_ref TEXT PRIMARY KEY', 'memory_id TEXT', 'profile_id TEXT', 'version INTEGER',
               'body TEXT', 'content_hash TEXT', 'source_hash TEXT', 'created_at TEXT',
               'expires_at TEXT', 'index_state TEXT']
    columns = [column for column in columns if column.split()[0] != missing]
    store._db.execute('CREATE TABLE memory_versions (' + ','.join(columns) + ')')
    before = list(store._db.execute('PRAGMA table_info(memory_versions)'))
    try:
        with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
            MemoryService(store, auto_load=False)
        assert list(store._db.execute('PRAGMA table_info(memory_versions)')) == before
    finally:
        store.close()


def test_repeated_same_source_does_not_extend_retention(memory):
    service, profile = memory
    evidence = source(service, profile)
    first = save(service, profile, sources=[evidence])
    again = save(service, profile, sources=[evidence], policy={"expires_at": timestamp(90)})
    assert again["memory_ref"] == first["memory_ref"]
    assert again["expires_at"] == first["expires_at"]


def test_paraphrasing_same_source_does_not_restart_long_term_retention(memory):
    service, profile = memory
    evidence = source(service, profile)
    first = save(service, profile, sources=[evidence], policy={"retention": "long_term"})
    second = save(service, profile, sources=[evidence], policy={"retention": "long_term"},
                  draft=candidate("연결 풀 설정의 확인 절차를 바꾸어 설명한다"))
    assert second["expires_at"] == first["expires_at"]


def test_extra_source_body_is_not_copied_into_selective_memory(memory):
    service, profile = memory
    evidence = source(service, profile, extra_payload="DO_NOT_DUPLICATE_THE_RAW_BODY")
    save(service, profile, sources=[evidence])
    body = service.store._db.execute("SELECT body FROM memory_versions").fetchone()[0]
    assert "DO_NOT_DUPLICATE_THE_RAW_BODY" not in body


def test_correction_replaces_current_version_and_old_ref_is_blocked(memory):
    service, profile = memory
    first = save(service, profile)
    revised = save(service, profile, draft=candidate("E_CONN_001 오류는 대상 DB 설정도 확인한다"),
                   sources=[source(service, profile, key="two")], memory_ref=first["memory_ref"])
    assert revised["memory_ref"] != first["memory_ref"]
    assert service.search(profile["profile_id"], "E_CONN_001")["items"][0]["memory_ref"] == revised["memory_ref"]
    with pytest.raises(StoreError, match="MEMORY_CONFLICT"):
        service.get(profile["profile_id"], first["memory_ref"])
    with pytest.raises(StoreError, match="MEMORY_CONFLICT"):
        service.invalidate(profile["profile_id"], first["memory_ref"], reason="반증", source_records=[source(service, profile, key="three")])


def test_policy_revocation_and_actual_target_change_block_recall(memory):
    service, profile = memory
    item = save(service, profile)
    service.store.put_profile("alpha", resources=profile["resources"], memory_policy=memory_policy(model_allowed=False))
    assert service.search(profile["profile_id"], "E_CONN_001")["items"] == []
    with pytest.raises(StoreError, match="ACCESS_DENIED"):
        service.get(profile["profile_id"], item["memory_ref"])
    changed = configured_resource()
    changed["target"]["project"] = "another/service"
    policy = memory_policy()
    policy.pop("target_bindings")
    service.store.put_profile("alpha", resources=[changed], memory_policy=policy)
    assert service.search(profile["profile_id"], "E_CONN_001")["items"] == []


def test_next_start_removes_bodies_and_indexes_after_retention_withdrawal(memory):
    service, profile = memory
    item = save(service, profile)
    service.store.put_profile("alpha", resources=profile["resources"], memory_policy=memory_policy(retain_allowed=False))
    MemoryService(service.store, auto_load=False)
    assert service.store._db.execute("SELECT body FROM memory_versions WHERE memory_ref=?", (item["memory_ref"],)).fetchone()[0] is None
    assert service.store._db.execute("SELECT count(*) FROM memory_fts WHERE memory_ref=?", (item["memory_ref"],)).fetchone()[0] == 0


def test_disallowed_instance_selection_is_rejected(memory):
    service, profile = memory
    with pytest.raises(StoreError, match="ACCESS_DENIED"):
        service.search(profile["profile_id"], "connection", instance_ids=["unregistered"])


@pytest.mark.parametrize("changes", [{"available": False}, {"retain_allowed": False},
                                     {"model_allowed": False}, {"fields": []}])
def test_unavailable_or_unapproved_source_never_becomes_memory(memory, changes):
    service, profile = memory
    with pytest.raises(StoreError, match="ACCESS_DENIED|RESULT_VALIDATION_FAILED"):
        save(service, profile, sources=[source(service, profile, **changes)])
    assert service.search(profile["profile_id"], "E_CONN_001")["items"] == []


def test_long_term_requires_independent_permission_from_every_source(memory):
    service, profile = memory
    with pytest.raises(StoreError, match="ACCESS_DENIED"):
        save(service, profile, sources=[source(service, profile, long_term_allowed=False)],
             policy={"retention": "long_term"})
    permitted = save(service, profile, sources=[source(service, profile, key="long")],
                     policy={"retention": "long_term"})
    assert datetime.fromisoformat(permitted["expires_at"]) > datetime.now(UTC) + timedelta(days=360)


def test_expiration_and_invalidation_block_search_before_physical_cleanup(memory):
    service, profile = memory
    item = save(service, profile)
    service.store._db.execute("UPDATE memory_versions SET expires_at=? WHERE memory_ref=?", (timestamp(-1), item["memory_ref"]))
    assert service.search(profile["profile_id"], "E_CONN_001")["items"] == []
    with pytest.raises(StoreError, match="RECORD_EXPIRED"):
        service.get(profile["profile_id"], item["memory_ref"])
    assert service.cleanup()["deleted"] == 1
    assert service.store._db.execute("SELECT body FROM memory_versions WHERE memory_ref=?", (item["memory_ref"],)).fetchone()[0] is None
    assert service.store._db.execute("SELECT count(*) FROM memory_fts WHERE memory_ref=?", (item["memory_ref"],)).fetchone()[0] == 0


def test_source_secret_is_not_repeated_in_canonical_body(memory):
    service, profile = memory
    saved = save(service, profile, draft=candidate("설정 password=known-secret-123 는 공유하지 않는다"))
    assert "known-secret-123" not in saved["statement"]
    assert "REDACTED" in saved["statement"]


def test_invalidating_current_version_blocks_every_index(memory):
    service, profile = memory
    item = save(service, profile)
    invalidated = service.invalidate(profile["profile_id"], item["memory_ref"], reason="사용자 반증",
                                     source_records=[source(service, profile, key="counter")])
    assert invalidated["memory_state"] == "invalidated"
    assert service.search(profile["profile_id"], "E_CONN_001")["items"] == []
    assert service.store._db.execute("SELECT count(*) FROM memory_fts WHERE memory_ref=?", (item["memory_ref"],)).fetchone()[0] == 0


class NumericalFixtureEmbedder:
    """A deterministic numerical fixture, not a semantic model or production fallback."""

    dimensions = 3
    model_id = "test-vectors-one"

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]

    def embed_document(self, text):
        return [1.0, 0.0, 0.0] if "\n연결" in text else [0.0, 1.0, 0.0]


def test_sqlite_vector_search_and_hybrid_ranking_use_current_versions_only(memory):
    old, profile = memory
    service = MemoryService(old.store, NumericalFixtureEmbedder())
    related = save(service, profile, draft=candidate("연결 풀을 점검한다"))
    save(service, profile, draft=candidate("배포 설정을 점검한다"),
         sources=[source(service, profile, key="deploy")])
    result = service.search(profile["profile_id"], "semantic-fixture-query", limit=1)
    assert result["retrieval_state"] == "hybrid"
    assert result["items"][0]["memory_ref"] == related["memory_ref"]
    assert related["index_state"] == "ready"
    newer = save(service, profile, draft=candidate("연결 대상을 다시 확인한다"),
                 sources=[source(service, profile, key="revision")], memory_ref=related["memory_ref"])
    found = service.search(profile["profile_id"], "semantic-fixture-query", limit=1)
    assert found["items"][0]["memory_ref"] == newer["memory_ref"]
    assert service.store._db.execute("SELECT count(*) FROM memory_vectors WHERE memory_ref=?", (related["memory_ref"],)).fetchone()[0] == 0


def test_model_change_is_reported_until_explicit_reindex(memory):
    original, profile = memory
    service = MemoryService(original.store, NumericalFixtureEmbedder())
    save(service, profile)
    other_embedder = NumericalFixtureEmbedder()
    other_embedder.model_id = "different-compatible-dimension-model"
    other = MemoryService(original.store, other_embedder)
    limited = other.search(profile["profile_id"], "semantic-fixture-query")
    assert limited["items"] == []
    assert limited["limitations"]
    assert other.reindex(profile["profile_id"]) == {"ready": 1, "pending": 0}
    assert other.search(profile["profile_id"], "semantic-fixture-query")["items"]


def test_long_term_knowledge_survives_regular_source_expiration_but_not_deletion(memory):
    service, profile = memory
    evidence = source(service, profile)
    item = save(service, profile, sources=[evidence], policy={"retention": "long_term"})
    service.store._db.execute("UPDATE records SET expires_at=? WHERE record_ref=?", (timestamp(-1), evidence["record_ref"]))
    service.store.cleanup()
    public = service.get(profile["profile_id"], item["memory_ref"])
    assert public["sources"] == []
    assert public["statement"] == candidate()["statement"]
    service.store._db.execute("DELETE FROM records WHERE record_ref=?", (evidence["record_ref"],))
    assert service.search(profile["profile_id"], "E_CONN_001")["items"] == []


def test_expired_knowledge_can_be_invalidated_without_restoring_the_body(memory):
    service, profile = memory
    item = save(service, profile)
    service.store._db.execute("UPDATE memory_versions SET expires_at=? WHERE memory_ref=?", (timestamp(-1), item["memory_ref"]))
    service.cleanup()
    invalidated = service.invalidate(profile["profile_id"], item["memory_ref"], reason="이전 지식 반증",
                                     source_records=[source(service, profile, key="counter")])
    assert invalidated["memory_state"] == "invalidated"


def test_local_model_is_not_downloaded_by_normal_loading(tmp_path):
    with pytest.raises(StoreError, match="MEMORY_SEARCH_INCOMPLETE"):
        LocalEmbedder.load(tmp_path / "not-prepared")
    assert not (tmp_path / "not-prepared").exists()


@pytest.mark.skipif(not os.environ.get("DAERI_TEST_MODEL_CACHE"), reason="Explicitly prepared local model is required")
def test_actual_local_multilingual_model_retrieves_without_network(memory, monkeypatch):
    def no_dns(*args, **kwargs):
        raise AssertionError("Normal embedding and retrieval must remain offline")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    original, profile = memory
    embedder = LocalEmbedder.load(os.environ["DAERI_TEST_MODEL_CACHE"])
    service = MemoryService(original.store, embedder)
    related = candidate("A saturated database connection pool requires checking active sessions and connection limits.")
    related["applicability"] = "운영 작업"
    expected = save(service, profile, draft=related, policy={"retention": "long_term", "retention_days": None})
    assert expected["expires_at"] is None
    unrelated = candidate("Release deployment naming follows the approved build identifier.")
    unrelated["applicability"] = "운영 작업"
    alternate = save(service, profile, draft=unrelated, sources=[source(service, profile, key="build", record_days=None)],
                     policy={"expires_at": None})
    assert alternate["retention"] == "source_bound" and alternate["expires_at"] is None
    result = service.search(profile["profile_id"], "데이터베이스 접속이 실패할 때 연결 수를 확인하는 방법", limit=1)
    assert result["retrieval_state"] == "hybrid"
    assert result["limitations"] == []
    assert result["items"][0]["memory_ref"] == expected["memory_ref"]
    with pytest.raises(StoreError, match="MEMORY_INDEX_INCOMPLETE"):
        embedder.embed_document("데이터베이스 " * 1000)


def exportable_source(service, profile, *, proven=True, kind="call_result"):
    target = {"server": "https://git.example.test", "project": "team/service"}
    binding = hashlib.sha256(json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    root = {"segments": []}
    resource = {"instance_id": "primary-db", "type": "Source", "adapter": "source_read", "target": target,
        "identity_ref": "test:reader", "connection": {"transport": "stdio", "command": sys.executable, "args": ["-c", "pass"]},
        "capabilities": [{"capability_id": "read", "tool_name": "get_repository_file", "constraints": {"fixed": {"project_id": "team/service"}}}],
        "policy": {"allowed_model_fields": [root], "export_fields": [root], "retained_fields": [root], "masking": [],
            "memory": {"categories": ["lesson"], "source_fields": [root], "context_sources": ["request"], "retention": "long_term", "allow_model_recall": True}}}
    current = service.store.put_profile("alpha", resources=[resource], memory_policy=memory_policy(
        target_bindings={"primary-db": binding}, exports_allowed=True))
    origin = source(service, current, target_bindings={"primary-db": binding}, export_allowed=proven)
    if kind != "request":
        origin["record_ref"] = service.store.write_record(origin["task_id"], kind, {"text": "공개 가능한 확인 내용"}, timestamp(30))
    item = save(service, current, sources=[origin], policy={"retention": "long_term", "target_bindings": {"primary-db": binding}})
    return current, resource, origin, item


def test_memory_export_requires_original_field_proof_and_current_permission(memory):
    service, profile = memory
    current, resource, origin, item = exportable_source(service, profile, proven=False)
    assert service.can_export(current["profile_id"], item["memory_ref"]) is False


def test_long_term_export_uses_saved_proof_after_source_expiration(memory):
    service, profile = memory
    current, resource, origin, item = exportable_source(service, profile)
    assert service.can_export(current["profile_id"], item["memory_ref"]) is True
    service.store.set_task_expiry(origin["task_id"], timestamp(-1))
    service.store.cleanup()
    assert service.can_export(current["profile_id"], item["memory_ref"]) is True
    resource["policy"]["export_fields"] = [{"segments": [{"key": "different-field"}]}]
    service.store.put_profile("alpha", resources=[resource], memory_policy=current["memory_policy"])
    assert service.can_export(current["profile_id"], item["memory_ref"]) is False


@pytest.mark.parametrize("changed", ["target", "mask", "global"])
def test_memory_export_does_not_restore_revoked_target_mask_or_global_policy(memory, changed):
    service, profile = memory
    current, resource, origin, item = exportable_source(service, profile)
    policy = current["memory_policy"]
    if changed == "target":
        resource["target"]["project"] = "another/service"
    elif changed == "mask":
        resource["policy"]["masking"] = [{"field": {"segments": [{"key": "text"}]}, "action": "redact"}]
    else:
        policy["exports_allowed"] = False
    service.store.put_profile("alpha", resources=[resource], memory_policy=policy)
    assert service.can_export(current["profile_id"], item["memory_ref"]) is False


@pytest.mark.parametrize("changed", ["deleted", "memory_off", "source_field", "mask", "actual_target", "context"])
def test_every_memory_read_rechecks_current_resource_policy(memory, changed):
    service, profile = memory
    current, resource, origin, item = exportable_source(service, profile, kind="request" if changed == "context" else "call_result")
    resources = [resource]
    if changed == "deleted":
        resources = []
    elif changed == "memory_off":
        resource["policy"]["memory"] = {"categories": [], "source_fields": [], "context_sources": [], "retention": "none", "allow_model_recall": False}
    elif changed == "source_field":
        resource["policy"]["memory"]["source_fields"] = [{"segments": [{"key": "other"}]}]
    elif changed == "mask":
        resource["policy"]["masking"] = [{"field": {"segments": [{"key": "text"}]}, "action": "redact"}]
    elif changed == "actual_target":
        resource["target"]["project"] = "new/actual-target"
    elif changed == "context":
        resource["policy"]["memory"]["context_sources"] = []
    policy = current["memory_policy"]
    if changed == "deleted":
        policy.update(instance_ids=[], target_bindings={})
    service.store.put_profile("alpha", resources=resources, memory_policy=policy)
    assert service.search(current["profile_id"], "E_CONN_001")["items"] == []
    with pytest.raises(StoreError, match="ACCESS_DENIED"):
        service.get(current["profile_id"], item["memory_ref"])
    assert service.can_export(current["profile_id"], item["memory_ref"]) is False


def unbounded_profile(service, profile):
    return service.store.put_profile("alpha", resources=profile["resources"],
        memory_policy=memory_policy(exports_allowed=True), long_term_memory_days=None)


@pytest.mark.parametrize("export_fields,mask,permitted", [
    ([{"segments": []}], None, True),
    ([{"segments": [{"key": "text"}]}], None, False),
    ([], None, False),
    ([{"segments": []}], {"segments": []}, False),
    ([{"segments": []}], {"segments": [{"key": "text"}]}, False),
    ([{"segments": []}], {"segments": [{"key": "text"}, {"key": "nested"}]}, False),
    ([{"segments": []}], {"segments": [{"key": "unrelated"}]}, True),
])
def test_plan_memory_export_checks_every_contributing_field_and_mask_intersection(memory, export_fields, mask, permitted):
    service, profile = memory
    profile = unbounded_profile(service, profile)
    first = source(service, profile, export_allowed=True)
    second = {**first, "fields": [{"segments": [{"key": "more"}]}],
        "record_ref": service.store.write_record(first["task_id"], "request", {"more": "추가 확인 자료"}, first["expires_at"])}
    item = save(service, profile, sources=[first, second], policy={"retention": "long_term", "retention_days": None})
    rule = {"instance_id": "primary-db", "export_fields": export_fields,
            "masking": [{"field": mask, "action": "redact"}] if mask is not None else []}
    assert service.can_export(profile["profile_id"], item["memory_ref"], resource_rules=[rule]) is permitted
    assert service.can_export(profile["profile_id"], item["memory_ref"], resource_rules=[]) is False
    service.store.set_task_expiry(first["task_id"], timestamp(-1))
    service.store.cleanup()
    assert service.get(profile["profile_id"], item["memory_ref"])["sources"] == []
    assert service.can_export(profile["profile_id"], item["memory_ref"], resource_rules=[rule]) is permitted


def test_unbounded_long_term_memory_survives_original_expiry_and_rebuilds_indexes(memory):
    original, profile = memory
    profile = unbounded_profile(original, profile)
    service = MemoryService(original.store, NumericalFixtureEmbedder(), auto_load=False)
    origin = source(service, profile, export_allowed=True)
    item = save(service, profile, sources=[origin], policy={"retention": "long_term", "retention_days": None})
    assert item["expires_at"] is None
    service.store.set_task_expiry(origin["task_id"], timestamp(-1))
    service.store.cleanup()
    assert service.cleanup()["deleted"] == 0
    assert service.get(profile["profile_id"], item["memory_ref"])["expires_at"] is None
    assert service.can_export(profile["profile_id"], item["memory_ref"]) is True
    assert service.search(profile["profile_id"], "E_CONN_001")["items"][0]["memory_ref"] == item["memory_ref"]
    assert service.reindex(profile["profile_id"]) == {"ready": 1, "pending": 0}


@pytest.mark.parametrize("reason", ["deleted_source", "permission", "invalidation"])
def test_unbounded_does_not_bypass_source_deletion_permission_or_invalidation(memory, reason):
    service, profile = memory
    profile = unbounded_profile(service, profile)
    origin = source(service, profile)
    item = save(service, profile, sources=[origin], policy={"retention": "long_term", "retention_days": None})
    if reason == "deleted_source":
        service.store._db.execute("DELETE FROM records WHERE record_ref=?", (origin["record_ref"],))
    elif reason == "permission":
        service.store.put_profile("alpha", resources=profile["resources"], long_term_memory_days=None,
                                 memory_policy=memory_policy(model_allowed=False))
    else:
        service.invalidate(profile["profile_id"], item["memory_ref"], reason="사용자가 잘못된 지식임을 확인",
                           source_records=[origin])
    assert service.search(profile["profile_id"], "E_CONN_001")["items"] == []
    with pytest.raises(StoreError):
        service.get(profile["profile_id"], item["memory_ref"])


def test_unbounded_memory_can_be_invalidated_after_permission_cleanup_removed_its_body(memory):
    service, profile = memory
    profile = unbounded_profile(service, profile)
    origin = source(service, profile)
    item = save(service, profile, sources=[origin], policy={"retention": "long_term", "retention_days": None})
    service.store.put_profile("alpha", resources=profile["resources"], long_term_memory_days=None,
                             memory_policy=memory_policy(model_allowed=False))
    assert service.cleanup()["deleted"] == 1
    result = service.invalidate(profile["profile_id"], item["memory_ref"], reason="현재 사용자 정정", source_records=[origin])
    assert result["memory_state"] == "invalidated"


@pytest.mark.parametrize("removed_by", ["invalidation", "permission_cleanup"])
def test_reusing_old_sources_cannot_restore_removed_unbounded_memory(memory, removed_by):
    service, profile = memory
    profile = unbounded_profile(service, profile)
    origin = source(service, profile)
    item = save(service, profile, sources=[origin], policy={"retention": "long_term", "retention_days": None})
    if removed_by == "invalidation":
        service.invalidate(profile["profile_id"], item["memory_ref"], reason="사용자가 정정한 이전 지식", source_records=[origin])
    else:
        service.store.put_profile("alpha", resources=profile["resources"], long_term_memory_days=None,
                                 memory_policy=memory_policy(model_allowed=False))
        service.cleanup()
        profile = unbounded_profile(service, profile)
    with pytest.raises(StoreError, match="MEMORY_CONFLICT"):
        save(service, profile, sources=[origin], policy={"retention": "long_term", "retention_days": None},
             draft=candidate("사용 차단된 원래 출처를 새 문장으로 다시 등록한다"))
    fresh = save(service, profile, sources=[source(service, profile, key="independently-verified")],
                 policy={"retention": "long_term", "retention_days": None})
    assert fresh["expires_at"] is None


def test_switching_default_to_unbounded_never_extends_or_revives_old_finite_memory(memory):
    service, profile = memory
    origin = source(service, profile)
    first = save(service, profile, sources=[origin], policy={"retention": "long_term"})
    profile = unbounded_profile(service, profile)
    second = save(service, profile, sources=[origin], policy={"retention": "long_term", "retention_days": None},
                  draft=candidate("같은 확인 자료의 연결 풀 절차를 다시 표현한다"))
    assert second["expires_at"] == first["expires_at"]
    service.store._db.execute("UPDATE memory_versions SET expires_at=?", (timestamp(-1),))
    service.cleanup()
    with pytest.raises(StoreError, match="RECORD_EXPIRED"):
        save(service, profile, sources=[origin], policy={"retention": "long_term", "retention_days": None})


@pytest.mark.parametrize("profile_days,approved_days,expected_days", [(None, 5, 5), (7, None, 7), (7, 3, 3)])
def test_unbounded_permission_still_respects_a_finite_profile_or_plan_limit(memory, profile_days, approved_days, expected_days):
    service, profile = memory
    profile = service.store.put_profile("alpha", resources=profile["resources"], memory_policy=memory_policy(),
                                        long_term_memory_days=profile_days)
    item = save(service, profile, policy={"retention": "long_term", "retention_days": approved_days})
    expiry = datetime.fromisoformat(item["expires_at"])
    assert datetime.now(UTC) + timedelta(days=expected_days - 0.01) < expiry <= datetime.now(UTC) + timedelta(days=expected_days)


def test_unbounded_default_does_not_make_source_bound_memory_permanent(memory):
    service, profile = memory
    profile = unbounded_profile(service, profile)
    origin = source(service, profile, expires_at=timestamp(2))
    item = save(service, profile, sources=[origin], policy={"retention_days": None})
    assert datetime.fromisoformat(item["expires_at"]) <= datetime.now(UTC) + timedelta(days=2)


@pytest.mark.parametrize("interrupt", [False, True])
def test_legacy_nonnull_expiry_schema_upgrades_atomically_with_vectors_and_fts(memory, interrupt):
    original, profile = memory
    service = MemoryService(original.store, NumericalFixtureEmbedder(), auto_load=False)
    item = save(service, profile)
    columns = "memory_ref,memory_id,profile_id,version,body,content_hash,source_hash,created_at,expires_at,index_state"
    legacy = tuple(service.store._db.execute(f"SELECT {columns} FROM memory_versions").fetchone())
    vector = tuple(service.store._db.execute("SELECT * FROM memory_vectors").fetchone())
    # Reproduce the released schema with data created through the real service.
    with service.store.transaction():
        service.store._db.execute("DROP TABLE memory_vectors")
        service.store._db.execute("DROP TABLE memory_versions")
        service.store._db.execute("""CREATE TABLE memory_versions (
            memory_ref TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memory_heads(memory_id),
            profile_id TEXT NOT NULL REFERENCES profiles(profile_id), version INTEGER NOT NULL,
            body TEXT, content_hash TEXT NOT NULL, source_hash TEXT NOT NULL,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, index_state TEXT NOT NULL,
            UNIQUE(memory_id,version))""")
        service.store._db.execute("""CREATE TABLE memory_vectors (
            memory_ref TEXT PRIMARY KEY REFERENCES memory_versions(memory_ref),
            model_id TEXT NOT NULL, dimensions INTEGER NOT NULL, embedding BLOB NOT NULL)""")
        service.store._db.execute("INSERT INTO memory_versions VALUES(?,?,?,?,?,?,?,?,?,?)", legacy)
        service.store._db.execute("INSERT INTO memory_vectors VALUES(?,?,?,?)", vector)
    reopened = Store(service.store.data_dir)
    try:
        if interrupt:
            reopened._db.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_DROP_TABLE and name == "memory_versions" else sqlite3.SQLITE_OK)
            with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
                MemoryService(reopened, NumericalFixtureEmbedder(), auto_load=False)
            assert next(row for row in reopened._db.execute("PRAGMA table_info(memory_versions)") if row[1] == "expires_at")[3] == 1
            assert tuple(reopened._db.execute("SELECT * FROM memory_vectors").fetchone()) == vector
            reopened._db.set_authorizer(None)
        restored = MemoryService(reopened, NumericalFixtureEmbedder(), auto_load=False)
        assert next(row for row in reopened._db.execute("PRAGMA table_info(memory_versions)") if row[1] == "expires_at")[3] == 0
        assert tuple(reopened._db.execute("SELECT * FROM memory_vectors").fetchone()) == vector
        assert reopened._db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert restored.search(profile["profile_id"], "E_CONN_001")["items"][0]["memory_ref"] == item["memory_ref"]
        assert restored.get(profile["profile_id"], item["memory_ref"])["expires_at"] == item["expires_at"]
        with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
            with reopened.transaction():
                reopened._db.execute("""INSERT INTO memory_versions(memory_ref,memory_id,profile_id,version,body,
                    content_hash,source_hash,created_at,expires_at,index_state)
                    SELECT 'duplicate-ref',memory_id,profile_id,version,body,content_hash,source_hash,created_at,expires_at,index_state FROM memory_versions""")
        current = unbounded_profile(restored, profile)
        permanent = save(restored, current, sources=[source(restored, current, key="after-upgrade")],
                         policy={"retention": "long_term", "retention_days": None})
        assert permanent["expires_at"] is None
    finally:
        reopened.close()


def test_source_bound_without_any_expiry_keeps_sources_and_reindexes(memory):
    original, profile = memory
    profile = unbounded_profile(original, profile)
    service = MemoryService(original.store, NumericalFixtureEmbedder(), auto_load=False)
    origin = source(service, profile, record_days=None, export_allowed=True)
    item = save(service, profile, sources=[origin], policy={"expires_at": None})
    assert item["retention"] == "source_bound" and item["expires_at"] is None
    assert [entry["record_ref"] for entry in item["sources"]] == [origin["record_ref"]]
    assert service.can_export(profile["profile_id"], item["memory_ref"]) is True
    assert service.cleanup()["deleted"] == 0
    assert service.reindex(profile["profile_id"]) == {"ready": 1, "pending": 0}
    assert service.search(profile["profile_id"], "E_CONN_001")["items"][0]["expires_at"] is None


@pytest.mark.parametrize("record_days,source_days,task_days,expected", [(3, None, None, 3), (None, 2, None, 2), (None, None, 4, 4), (5, 4, 2, 2)])
def test_source_bound_intersects_finite_and_unbounded_origin_and_task_expiry(memory, record_days, source_days, task_days, expected):
    service, profile = memory
    origin = source(service, profile, record_days=record_days,
                    expires_at=timestamp(source_days) if source_days is not None else None)
    item = save(service, profile, sources=[origin], policy={"expires_at": timestamp(task_days) if task_days is not None else None})
    expiry = datetime.fromisoformat(item["expires_at"])
    assert datetime.now(UTC) + timedelta(days=expected - 0.01) < expiry <= datetime.now(UTC) + timedelta(days=expected)


def test_original_expiry_removal_cannot_extend_or_revive_a_finite_source_bound_memory(memory):
    service, profile = memory
    origin = source(service, profile, record_days=None)
    first = save(service, profile, sources=[origin], policy={"expires_at": timestamp(2)})
    revised = save(service, profile, sources=[origin], policy={"expires_at": None},
                   draft=candidate("동일한 원출처를 다시 표현한 연결 점검 절차"))
    assert revised["expires_at"] == first["expires_at"]
    service.store._db.execute("UPDATE memory_versions SET expires_at=?", (timestamp(-1),))
    service.cleanup()
    with pytest.raises(StoreError, match="RECORD_EXPIRED"):
        save(service, profile, sources=[origin], policy={"expires_at": None})


def test_unbounded_source_still_requires_verification_time_and_respects_explicit_block(memory):
    service, profile = memory
    origin = source(service, profile, record_days=None)
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        save(service, profile, sources=[{**origin, "verified_at": None}], policy={"expires_at": None})
    item = save(service, profile, sources=[origin], policy={"expires_at": None})
    service.store._db.execute("UPDATE records SET blocked=1 WHERE record_ref=?", (origin["record_ref"],))
    assert service.search(profile["profile_id"], "E_CONN_001")["items"] == []
    with pytest.raises(StoreError, match="ACCESS_DENIED"):
        service.get(profile["profile_id"], item["memory_ref"])
