from datetime import UTC, datetime, timedelta
import os
import socket
import hashlib
import json
import sys

import pytest

from daeri.memory import LocalEmbedder, MemoryService
from daeri.storage import Store, StoreError


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


def source(service, profile, *, key="one", **changes):
    store = service.store
    task_id = "task-" + key
    store.insert_task({"task_id": task_id, "profile_id": profile["profile_id"],
                       "task_revision": 1, "state": "succeeded", "created_at": timestamp(),
                       "last_report_ref": "report-existing"})
    ref = store.write_record(task_id, "request", {"text": "실제 확인한 서비스 용어"}, timestamp(30))
    return {"task_id": task_id, "record_ref": ref,
            "fields": [{"segments": [{"key": "text"}]}],
            "instance_ids": ["primary-db"], "target_bindings": {"primary-db": TARGET_HASH},
            "verified_at": timestamp(), "expires_at": timestamp(30), "available": True,
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
    expected = save(service, profile, draft=related)
    unrelated = candidate("Release deployment naming follows the approved build identifier.")
    unrelated["applicability"] = "운영 작업"
    save(service, profile, draft=unrelated, sources=[source(service, profile, key="build")])
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
