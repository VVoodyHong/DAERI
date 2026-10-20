"""Memory provenance survives blocking, and model work does not hold SQLite writers."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from daeri.engine import Engine
from daeri.memory import MemoryService
from daeri.storage import Store, StoreError
from test_engine import create, draft, request_approval, response_args
from test_memory import NumericalFixtureEmbedder, candidate, configured_resource, memory_policy, save, source


@pytest.fixture
def completed(tmp_path):
    engine = Engine(tmp_path)
    resource = configured_resource()
    profile = engine.store.put_profile("memory-regression", resources=[resource],
                                      memory_policy=memory_policy(), long_term_memory_days=7)
    task = create(engine, profile)
    answered = engine.call("daeri_task_update", {"request_id": "answer", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"], "patch": {"clarification": "사용자가 확인한 운영 규칙"}})["data"]
    references = [task["request_ref"], answered["clarification_ref"]]
    searched = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "운영 규칙"})["data"]
    planned = draft(searched["memory_search_ref"])
    planned["output"]["resource_rules"] = [{"instance_id": resource["instance_id"], **copy.deepcopy(resource["policy"])}]
    saved = engine.call("daeri_plan_submit", {"request_id": "plan", "task_id": task["task_id"],
        "expected_task_revision": answered["task_revision"], "draft": planned})
    assert saved["status"] == "ok", saved
    saved = saved["data"]
    approved = engine.call("daeri_approval_request", response_args(saved, request_approval(engine, saved)))
    assert approved["status"] == "ok", approved
    report = engine.call("daeri_result_submit", {"request_id": "report", "task_id": task["task_id"],
        "expected_task_revision": approved["data"]["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"],
        "report": {"summary": "사용자 설명을 확인했다", "proposed_task_state": "succeeded",
            "findings": [{"kind": "observation", "text": "사용자 확인", "evidence_refs": references}], "tables": [],
            "checks": [{"criterion_id": "explanation", "status": "met", "method": "사용자 설명 대조", "evidence_refs": references}],
            "limitations": [], "uncertain_effects": [], "next_actions": [], "artifacts": []}})
    assert report["status"] == "ok", report
    args = {"request_id": "remember", "task_id": task["task_id"], "expected_task_revision": report["data"]["task_revision"],
            "operation": "store", "source_refs": references, "reason": "확인한 지식 재사용", "draft": candidate()}
    yield engine, profile, args
    engine.close()


def test_public_update_cannot_restore_invalidated_knowledge_with_an_old_source_subset(completed):
    engine, profile, args = completed
    saved = engine.call("daeri_memory_update", args)["data"]
    blocked = engine.call("daeri_memory_update", {"request_id": "invalidate", "task_id": args["task_id"],
        "expected_task_revision": saved["task_revision"], "operation": "invalidate", "source_refs": args["source_refs"][1:],
        "reason": "사용자 정정으로 이전 규칙 사용 중단", "memory_ref": saved["memory_ref"]})
    assert blocked["data"]["memory_state"] == "invalidated", blocked
    result = engine.call("daeri_memory_update", {**args, "request_id": "old-subset",
        "expected_task_revision": blocked["data"]["task_revision"], "source_refs": args["source_refs"][:1]})
    assert result["status"] == "error", result
    assert result["error"]["code"] == "MEMORY_CONFLICT"
    assert engine.memory.search(profile["profile_id"], "E_CONN_001")["items"] == []


def test_adding_an_older_source_does_not_restart_retention_but_a_new_observation_can(tmp_path, monkeypatch):
    from daeri import memory

    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(memory, "datetime", Clock)
    store = Store(tmp_path)
    try:
        profile = store.put_profile("alpha", resources=[configured_resource()], memory_policy=memory_policy(), long_term_memory_days=7)
        service = MemoryService(store, auto_load=False)
        first_source = source(service, profile, key="first", verified_at=(Clock.current - timedelta(seconds=2)).isoformat())
        older_source = source(service, profile, key="older", verified_at=(Clock.current - timedelta(seconds=1)).isoformat())
        first = save(service, profile, sources=[first_source], policy={"retention": "long_term", "retention_days": 7})
        Clock.current += timedelta(days=1)
        revised = save(service, profile, sources=[first_source, older_source], memory_ref=first["memory_ref"],
                       draft=candidate("이전 두 자료로만 절차를 다시 표현한다"), policy={"retention": "long_term", "retention_days": 7})
        assert revised["expires_at"] == first["expires_at"]
        Clock.current += timedelta(seconds=1)
        fresh = source(service, profile, key="fresh", verified_at=Clock.current.isoformat())
        checked = save(service, profile, sources=[fresh], memory_ref=revised["memory_ref"],
                       draft=candidate("새 관찰로 독립적으로 다시 확인한 절차"), policy={"retention": "long_term", "retention_days": 7})
        assert datetime.fromisoformat(checked["expires_at"]) == Clock.current + timedelta(days=7)
    finally:
        store.close()


@pytest.mark.parametrize("cleanup_first", [False, True])
@pytest.mark.parametrize("retention", ["source_bound", "long_term"])
def test_fresh_observation_after_expiry_is_accepted_before_and_after_regular_cleanup(tmp_path, monkeypatch, cleanup_first, retention):
    from daeri import memory

    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(memory, "datetime", Clock)
    store = Store(tmp_path)
    try:
        profile = store.put_profile("alpha", resources=[configured_resource()], memory_policy=memory_policy(), long_term_memory_days=7)
        service = MemoryService(store, auto_load=False)
        original = source(service, profile, key="original", record_days=7, verified_at=Clock.current.isoformat())
        saved = save(service, profile, sources=[original], policy={"retention": retention, "retention_days": 7,
            "expires_at": (Clock.current + timedelta(days=7)).isoformat()})
        Clock.current += timedelta(days=8)
        fresh = source(service, profile, key="fresh", verified_at=Clock.current.isoformat())
        Clock.current += timedelta(days=1)
        if cleanup_first:
            assert service.cleanup()["deleted"] == 1
        checked = save(service, profile, sources=[fresh], policy={"retention": retention, "retention_days": 7,
            "expires_at": (Clock.current + timedelta(days=7)).isoformat()})
        assert checked["memory_ref"] != saved["memory_ref"]
        assert datetime.fromisoformat(checked["expires_at"]) == Clock.current + timedelta(days=7)
    finally:
        store.close()


def test_expired_memory_explicitly_invalidated_still_requires_observation_after_that_block(tmp_path, monkeypatch):
    from daeri import memory

    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(memory, "datetime", Clock)
    store = Store(tmp_path)
    try:
        profile = store.put_profile("alpha", resources=[configured_resource()], memory_policy=memory_policy(), long_term_memory_days=7)
        service = MemoryService(store, auto_load=False)
        original = source(service, profile, key="original", verified_at=Clock.current.isoformat())
        saved = save(service, profile, sources=[original], policy={"retention": "long_term", "retention_days": 7})
        Clock.current += timedelta(days=8)
        before_block = source(service, profile, key="before-block", verified_at=Clock.current.isoformat())
        Clock.current += timedelta(days=1)
        service.invalidate(profile["profile_id"], saved["memory_ref"], reason="사용자 정정으로 명시적 차단", source_records=[before_block])
        service.cleanup()
        with pytest.raises(StoreError, match="MEMORY_CONFLICT"):
            save(service, profile, sources=[before_block], policy={"retention": "long_term", "retention_days": 7})
        Clock.current += timedelta(days=1)
        fresh = source(service, profile, key="after-block", verified_at=Clock.current.isoformat())
        checked = save(service, profile, sources=[fresh], policy={"retention": "long_term", "retention_days": 7})
        assert datetime.fromisoformat(checked["expires_at"]) == Clock.current + timedelta(days=7)
    finally:
        store.close()


def test_expired_memory_with_current_policy_revocation_requires_a_later_observation(tmp_path, monkeypatch):
    from daeri import memory

    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(memory, "datetime", Clock)
    store = Store(tmp_path)
    try:
        profile = store.put_profile("alpha", resources=[configured_resource()], memory_policy=memory_policy(), long_term_memory_days=7)
        service = MemoryService(store, auto_load=False)
        original = source(service, profile, key="original", verified_at=Clock.current.isoformat())
        save(service, profile, sources=[original], policy={"retention": "long_term", "retention_days": 7})
        Clock.current += timedelta(days=8)
        before_block = source(service, profile, key="before-policy-block", verified_at=Clock.current.isoformat())
        Clock.current += timedelta(days=1)
        store.put_profile("alpha", resources=profile["resources"], memory_policy=memory_policy(model_allowed=False))
        assert service.cleanup()["deleted"] == 1
        store.put_profile("alpha", resources=profile["resources"], memory_policy=memory_policy())
        with pytest.raises(StoreError, match="MEMORY_CONFLICT|RECORD_EXPIRED"):
            save(service, profile, sources=[before_block], policy={"retention": "long_term", "retention_days": 7})
        Clock.current += timedelta(days=1)
        fresh = source(service, profile, key="after-policy-block", verified_at=Clock.current.isoformat())
        checked = save(service, profile, sources=[fresh], policy={"retention": "long_term", "retention_days": 7})
        assert datetime.fromisoformat(checked["expires_at"]) == Clock.current + timedelta(days=7)
    finally:
        store.close()


class WaitingEmbedder(NumericalFixtureEmbedder):
    def __init__(self, operation):
        self.operation, self.entered, self.release = operation, Event(), Event()

    def _wait(self, operation):
        if operation == self.operation:
            self.entered.set()
            assert self.release.wait(5), "The test must release the local model operation."

    def embed_query(self, text):
        self._wait("query")
        return super().embed_query(text)

    def embed_document(self, text):
        self._wait("document")
        return super().embed_document(text)


def test_public_search_allows_other_host_reads_and_rejects_policy_changes_during_embedding(completed):
    engine, profile, args = completed
    embedder = WaitingEmbedder("query")
    engine.memory = MemoryService(engine.store, embedder, auto_load=False)
    other = Store(engine.store.data_dir)
    other._db.execute("PRAGMA busy_timeout=100")
    before = engine.store._db.execute("SELECT count(*) FROM records WHERE kind='memory_search'").fetchone()[0]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine.call, "daeri_memory_search", {"task_id": args["task_id"], "query": "운영 규칙"})
            assert embedder.entered.wait(2)
            try:
                assert other.get_profile(profile["profile_id"])["revision"] == profile["revision"]
                other.put_profile(profile["name"], resources=profile["resources"], memory_policy=memory_policy(model_allowed=False))
            finally:
                embedder.release.set()
            result = future.result(timeout=5)
        assert result["status"] == "error", result
        assert result["error"]["code"] == "CONTEXT_CHANGED"
        assert engine.store._db.execute("SELECT count(*) FROM records WHERE kind='memory_search'").fetchone()[0] == before
    finally:
        other.close()


def test_update_commits_pending_memory_before_embedding_and_rechecks_current_policy(completed):
    engine, profile, args = completed
    embedder = WaitingEmbedder("document")
    engine.memory = MemoryService(engine.store, embedder, auto_load=False)
    other = Store(engine.store.data_dir)
    other._db.execute("PRAGMA busy_timeout=100")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine.call, "daeri_memory_update", args)
            assert embedder.entered.wait(2)
            try:
                assert other.get_profile(profile["profile_id"])["revision"] == profile["revision"]
                canonical = other._db.execute("SELECT body,index_state FROM memory_versions").fetchone()
                assert canonical["body"] is not None and canonical["index_state"] == "pending"
                other.put_profile(profile["name"], resources=profile["resources"], memory_policy=memory_policy(model_allowed=False))
            finally:
                embedder.release.set()
            result = future.result(timeout=5)
        assert result["status"] == "error", result
        assert result["error"]["code"] == "CONTEXT_CHANGED"
        row = other._db.execute("SELECT body,index_state FROM memory_versions").fetchone()
        assert row["body"] is not None and row["index_state"] == "pending"
        assert other._db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 0
        assert other.get_task(args["task_id"])["task_revision"] == args["expected_task_revision"] + 1
    finally:
        other.close()


def test_reindex_cannot_publish_after_another_host_invalidates_the_memory(tmp_path):
    store = Store(tmp_path)
    other = Store(tmp_path)
    try:
        profile = store.put_profile("alpha", resources=[configured_resource()], memory_policy=memory_policy())
        service = MemoryService(store, auto_load=False)
        evidence = source(service, profile)
        saved = save(service, profile, sources=[evidence])
        embedder = WaitingEmbedder("document")
        service = MemoryService(store, embedder, auto_load=False)
        other_service = MemoryService(other, auto_load=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(service.reindex, profile["profile_id"])
            assert embedder.entered.wait(2)
            try:
                other_service.invalidate(profile["profile_id"], saved["memory_ref"], reason="새 사용자 정정", source_records=[evidence])
            finally:
                embedder.release.set()
            assert future.result(timeout=5) == {"ready": 0, "pending": 1}
        assert store._db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 0
        assert store._db.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 0
        with pytest.raises(StoreError, match="MEMORY_CONFLICT"):
            service.get(profile["profile_id"], saved["memory_ref"])
    finally:
        other.close()
        store.close()


@pytest.mark.parametrize("retained_body", [True, False])
def test_legacy_lineage_blocks_old_subsets_and_accepts_fresh_independent_evidence(tmp_path, retained_body):
    store = Store(tmp_path)
    try:
        profile = store.put_profile("alpha", resources=[configured_resource()], memory_policy=memory_policy())
        service = MemoryService(store, auto_load=False)
        first, second = source(service, profile, key="first"), source(service, profile, key="second")
        saved = save(service, profile, sources=[first, second])
        if not retained_body:
            service.invalidate(profile["profile_id"], saved["memory_ref"], reason="사용자 정정", source_records=[second])
        with store.transaction():
            store._db.execute("ALTER TABLE memory_versions DROP COLUMN source_lineage")
        restored = MemoryService(store, auto_load=False)
        if retained_body:
            restored.invalidate(profile["profile_id"], saved["memory_ref"], reason="사용자 정정", source_records=[second])
        with pytest.raises(StoreError, match="MEMORY_CONFLICT"):
            save(restored, profile, sources=[first], draft=candidate("옛 자료의 부분집합으로 지식을 다시 표현한다"))
        fresh = source(restored, profile, key="independent")
        checked = save(restored, profile, sources=[fresh], draft=candidate("새 사용자 확인으로 검증한 별도 지식"))
        assert checked["statement"] == "새 사용자 확인으로 검증한 별도 지식"
    finally:
        store.close()


def test_public_update_reports_late_invalidation_without_publishing_its_vector(completed):
    engine, profile, args = completed
    embedder = WaitingEmbedder("document")
    engine.memory = MemoryService(engine.store, embedder, auto_load=False)
    other = Store(engine.store.data_dir)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(engine.call, "daeri_memory_update", args)
            assert embedder.entered.wait(2)
            try:
                other_service = MemoryService(other, auto_load=False)
                reference = other._db.execute("SELECT memory_ref FROM memory_versions").fetchone()[0]
                counter = source(other_service, profile, key="counter")
                other_service.invalidate(profile["profile_id"], reference, reason="실제 사용자 정정", source_records=[counter])
            finally:
                embedder.release.set()
            result = future.result(timeout=5)
        assert result["status"] == "ok", result
        assert result["data"]["memory_state"] == "invalidated"
        assert other._db.execute("SELECT count(*) FROM memory_vectors").fetchone()[0] == 0
        assert other._db.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 0
        assert other._db.execute("SELECT body FROM memory_versions").fetchone()[0] is None
    finally:
        other.close()


def test_public_update_preserves_pending_memory_and_replays_after_index_failure(completed):
    class FailedModel(NumericalFixtureEmbedder):
        def embed_document(self, text):
            raise RuntimeError("controlled local inference failure")

    engine, profile, args = completed
    engine.memory = MemoryService(engine.store, FailedModel(), auto_load=False)
    result = engine.call("daeri_memory_update", args)
    assert result["status"] == "partial", result
    assert result["data"]["memory_state"] == "active" and result["data"]["index_state"] == "pending"
    assert engine.memory.get(profile["profile_id"], result["data"]["memory_ref"])["statement"] == args["draft"]["statement"]
    repeated = engine.call("daeri_memory_update", args)
    assert repeated["data"] == result["data"]
    assert engine.store._db.execute("SELECT count(*) FROM memory_versions").fetchone()[0] == 1
