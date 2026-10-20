import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from daeri.knowledge import replay, update
from daeri.memory import MemoryService
from daeri.storage import Store, StoreError


def select(*parts):
    return {"segments": [{"each": True} if value is Ellipsis else {"index": value} if type(value) is int else {"key": value} for value in parts]}


def timestamp(days=0):
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


class LocalEngine:
    def __init__(self, directory):
        self.store = Store(directory)
        target = {"server": "https://git.example.test", "project": "team/service"}
        target_hash = hashlib.sha256(json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        memory_rule = {"categories": ["service_rule", "lesson"], "source_fields": [select()],
                       "context_sources": ["request", "clarification"], "retention": "long_term", "allow_model_recall": True}
        self.resource = {"instance_id": "source-one", "type": "Source", "adapter": "source_read", "target": target,
            "identity_ref": "identity:reader", "connection": {"transport": "stdio", "command": sys.executable, "args": ["-c", "pass"]},
            "capabilities": [{"capability_id": "read", "tool_name": "get_repository_file", "constraints": {"fixed": {"project_id": "team/service"}}}],
            "policy": {"allowed_model_fields": [select()], "export_fields": [select()], "retained_fields": [select()], "masking": [], "memory": memory_rule}}
        self.profile = self.store.put_profile("knowledge-profile", resources=[self.resource], memory_policy={
            "categories": ["service_rule", "lesson"], "instance_ids": ["source-one"], "target_bindings": {"source-one": target_hash},
            "model_allowed": True, "retain_allowed": True, "long_term_allowed": True, "exports_allowed": True})
        self.task = {"task_id": "knowledge-task", "profile_id": self.profile["profile_id"], "task_revision": 1,
            "state": "succeeded", "owner_id": "self", "finished_at": timestamp(),
            "context": {"profile_revision": 1}, "active_plan_ref": {"plan_id": "plan-one", "plan_version": 1},
            "profile_snapshot": {"retention_days": 30, "long_term_memory_days": 365},
            "approvals": [{"plan_id": "plan-one", "plan_version": 1, "ever_approved": True, "state": "approved"}], "last_report_ref": None}
        self.store.insert_task(self.task)
        self.task["last_report_ref"] = self.store.write_record(self.task["task_id"], "report", {"summary": "완료"}, timestamp(30))
        self.plan = {"plan_id": "plan-one", "plan_version": 1, "draft": {"output": {"resource_rules": [{"instance_id": "source-one", **copy.deepcopy(self.resource["policy"])}]},
            "carried_evidence_refs": [], "steps": [{"step_id": "collect", "instance_id": "source-one", "capability_id": "read", "contract_ref": "read-contract"}]}}
        self.memory = MemoryService(self.store, auto_load=False)
        self.records = {}

    def _own(self, task):
        if task.get("owner_id") != "self":
            raise StoreError("OWNER_CONFLICT", "Owner mismatch")

    def _save(self, task):
        old = task["task_revision"]
        task["task_revision"] += 1
        self.store.update_task(task, old)

    def _current_profile(self, task):
        return self.profile

    def _plan_get(self, args, task):
        return {"status": "ok", "data": {"plan": self.plan}, "error": None}

    def _load_evidence(self, task, reference, mode="model"):
        self.store.read_record(task["task_id"], reference)
        value = copy.deepcopy(self.records[reference])
        if mode == "export":
            value["allowed_paths"] = value.get("export_allowed_paths", value.get("allowed_paths", []))
        return value

    def record(self, kind="call_result", *, payload=None, **changes):
        result = {"rows": [{"term": "결제 완료 기준", "meaning": "확인한 필드 의미"}]}
        payload = ({"result": result, "instance_id": "source-one", "step_id": "collect",
                    "contract_ref": "read-contract", "plan_id": "plan-one", "plan_version": 1,
                    "observed_at": timestamp(-1)} if payload is None and kind == "call_result" else payload or {"request_text": "사용자가 확인한 용어"})
        ref = self.store.write_record(self.task["task_id"], kind, payload, changes.get("expires_at", timestamp(30)))
        fields = ["/rows/0/term", "/rows/0/meaning"] if kind == "call_result" else ["/request_text"]
        record = {"record_kind": kind, "payload": payload, "provided_paths": fields, "retained_paths": fields,
                  "allowed_paths": fields, "expires_at": timestamp(30), **changes}
        if kind == "call_result":
            record.update({key: payload[key] for key in ("result", "instance_id", "step_id", "contract_ref", "plan_id", "plan_version", "observed_at")})
        self.records[ref] = record
        return ref


@pytest.fixture
def runtime(tmp_path):
    engine = LocalEngine(tmp_path)
    yield engine
    engine.store.close()


def arguments(engine, references, **changes):
    return {"request_id": "update-one", "task_id": engine.task["task_id"], "expected_task_revision": engine.task["task_revision"],
        "operation": "store", "source_refs": references, "reason": "확인한 서비스 용어 재사용",
        "draft": {"category": "service_rule", "statement": "결제 완료는 확인한 상태 필드로 판단한다", "applicability": "이 서비스의 주문 집계",
                  "instance_ids": ["source-one"], "verification_method": "실제 조회한 필드 의미와 대조"}, **changes}


def test_selective_memory_update_binds_actual_fields_without_copying_candidate_into_receipt(runtime):
    ref = runtime.record()
    args = arguments(runtime, [ref])
    result = update(runtime, args, runtime.task)
    assert result["status"] == "partial"
    assert result["error"]["code"] == "MEMORY_INDEX_INCOMPLETE"
    assert result["data"]["memory_state"] == "active"
    assert result["data"]["task_revision"] == 2
    assert runtime.task["state"] == "succeeded"
    item = runtime.memory.get(runtime.profile["profile_id"], result["data"]["memory_ref"])
    assert item["retention"] == "long_term"
    assert item["sources"][0]["fields"] == [select("rows", 0, "meaning"), select("rows", 0, "term")]
    receipt = runtime.store.read_record(runtime.task["task_id"], result["data"]["update_ref"])
    assert args["draft"]["statement"] not in json.dumps(receipt, ensure_ascii=False)


@pytest.mark.parametrize("which", ["memory_source_fields", "provided_retention", "current_access", "category", "approval", "report", "terminal", "owner", "secret"])
def test_memory_does_not_bypass_plan_actual_source_or_owner(runtime, which):
    ref = runtime.record()
    args = arguments(runtime, [ref])
    if which == "memory_source_fields":
        runtime.plan["draft"]["output"]["resource_rules"][0]["memory"]["source_fields"] = [select("rows", Ellipsis, "term")]
    elif which == "provided_retention":
        runtime.records[ref]["retained_paths"] = ["/rows/0/term"]
    elif which == "current_access":
        runtime.records[ref]["allowed_paths"] = ["/rows/0/term"]
    elif which == "category":
        runtime.plan["draft"]["output"]["resource_rules"][0]["memory"]["categories"] = ["lesson"]
    elif which == "approval":
        runtime.task["approvals"] = []
    elif which == "report":
        runtime.task["last_report_ref"] = None
    elif which == "terminal":
        runtime.task["state"] = "running"
    elif which == "owner":
        runtime.task["owner_id"] = "another-host"
    elif which == "secret":
        args["draft"]["statement"] = "password=DO_NOT_PERSIST"
    with pytest.raises(StoreError):
        update(runtime, args, runtime.task)
    assert runtime.store._db.execute("SELECT count(*) FROM memory_versions").fetchone()[0] == 0
    assert runtime.task["task_revision"] == 1


def test_user_context_requires_explicit_context_source_permission(runtime):
    ref = runtime.record("request")
    result = update(runtime, arguments(runtime, [ref]), runtime.task)
    assert result["data"]["memory_state"] == "active"
    runtime.plan["draft"]["output"]["resource_rules"][0]["memory"]["context_sources"] = []
    with pytest.raises(StoreError, match="ACCESS_DENIED"):
        update(runtime, arguments(runtime, [ref], request_id="second"), runtime.task)


def test_report_and_table_sources_resolve_to_actual_terminal_observations(runtime):
    leaf = runtime.record()
    table = runtime.record("table", payload={"sources": [{"source_id": "one", "record_refs": [leaf]}]}, provided_paths=[], retained_paths=[], allowed_paths=[])
    report = runtime.record("report", payload={"findings": [{"kind": "derived", "text": "계산 결과", "evidence_refs": [], "table_ids": ["summary"]}],
        "table_validations": [{"table_id": "summary", "source_refs": [leaf]}]}, provided_paths=[], retained_paths=[], allowed_paths=[])
    result = update(runtime, arguments(runtime, [table, report]), runtime.task)
    item = runtime.memory.get(runtime.profile["profile_id"], result["data"]["memory_ref"])
    assert [source["record_ref"] for source in item["sources"]] == [leaf]


def test_hypothesis_and_cycles_cannot_become_verified_memory(runtime):
    hypothesis = runtime.record("report", payload={"findings": [{"kind": "hypothesis", "text": "추정 원인", "evidence_refs": []}]})
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        update(runtime, arguments(runtime, [hypothesis]), runtime.task)
    cycle = runtime.record("table", payload={"evidence_refs": []})
    runtime.records[cycle]["payload"]["evidence_refs"] = [cycle]
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        update(runtime, arguments(runtime, [cycle]), runtime.task)


def test_invalidation_accepts_counterevidence_without_permission_to_store_new_memory(runtime):
    stored = update(runtime, arguments(runtime, [runtime.record()]), runtime.task)
    current = stored["data"]["memory_ref"]
    runtime.task["state"] = "planning"
    runtime.plan["draft"]["output"]["resource_rules"][0]["memory"] = {"categories": [], "source_fields": [], "context_sources": [], "retention": "none", "allow_model_recall": False}
    args = arguments(runtime, [runtime.record("request")], operation="invalidate", memory_ref=current)
    args.pop("draft")
    result = update(runtime, args, runtime.task)
    assert result["status"] == "ok" and result["data"]["memory_state"] == "invalidated"
    assert runtime.memory.search(runtime.profile["profile_id"], "결제")["items"] == []


def test_replay_returns_current_index_state_without_new_version_or_retention(runtime):
    args = arguments(runtime, [runtime.record()])
    first = update(runtime, args, runtime.task)
    runtime.store._db.execute("UPDATE memory_versions SET index_state='ready'")
    result = replay(runtime, args, first)
    assert result["status"] == "ok"
    assert result["data"]["expires_at"] == first["data"]["expires_at"]
    assert result["data"]["index_state"] == "ready"
    assert runtime.store._db.execute("SELECT count(*) FROM memory_versions").fetchone()[0] == 1
    assert runtime.task["task_revision"] == 2


def test_counter_observation_for_another_actual_target_does_not_invalidate_memory(runtime):
    stored = update(runtime, arguments(runtime, [runtime.record()]), runtime.task)
    other = copy.deepcopy(runtime.resource)
    other.update(instance_id="source-other", target={"server": "https://git.example.test", "project": "another/service"})
    runtime.profile["resources"].append(other)
    reference = runtime.record()
    runtime.records[reference]["instance_id"] = "source-other"
    args = arguments(runtime, [reference], operation="invalidate", memory_ref=stored["data"]["memory_ref"])
    args.pop("draft")
    with pytest.raises(StoreError, match="ACCESS_DENIED"):
        update(runtime, args, runtime.task)


def test_duplicate_candidate_uses_stable_source_verification_time(runtime):
    reference = runtime.record("request")
    args = arguments(runtime, [reference])
    first = update(runtime, args, runtime.task)
    args.update(request_id="second", expected_task_revision=runtime.task["task_revision"])
    second = update(runtime, args, runtime.task)
    assert second["data"]["memory_ref"] == first["data"]["memory_ref"]
    assert second["data"]["expires_at"] == first["data"]["expires_at"]


def test_replay_does_not_present_invalidated_memory_as_active(runtime):
    args = arguments(runtime, [runtime.record()])
    first = update(runtime, args, runtime.task)
    invalidation = arguments(runtime, [runtime.record("request")], operation="invalidate", memory_ref=first["data"]["memory_ref"])
    invalidation.pop("draft")
    update(runtime, invalidation, runtime.task)
    result = replay(runtime, args, first)
    assert result["data"]["memory_state"] == "invalidated"
    assert result["data"]["expires_at"] is None


def test_memory_and_receipt_roll_back_when_task_revision_cannot_be_saved(runtime):
    def fail(task):
        raise StoreError("STORE_UNAVAILABLE", "Interrupted task revision")
    runtime._save = fail
    before = runtime.store.list_records(runtime.task["task_id"])
    reference = runtime.record()
    with pytest.raises(StoreError, match="STORE_UNAVAILABLE"):
        update(runtime, arguments(runtime, [reference]), runtime.task)
    assert runtime.store._db.execute("SELECT count(*) FROM memory_versions").fetchone()[0] == 0
    assert all(value["record_kind"] != "memory_update" for value in runtime.store.list_records(runtime.task["task_id"]))


@pytest.mark.parametrize("allowed", [True, False])
def test_memory_records_original_export_proof_independently_of_model_recall(runtime, allowed):
    reference = runtime.record()
    if not allowed:
        runtime.records[reference]["export_allowed_paths"] = ["/rows/0/term"]
    stored = update(runtime, arguments(runtime, [reference]), runtime.task)
    memory_ref = stored["data"]["memory_ref"]
    assert runtime.memory.get(runtime.profile["profile_id"], memory_ref)["statement"]
    assert runtime.memory.can_export(runtime.profile["profile_id"], memory_ref) is allowed


def test_unbounded_memory_update_and_replay_keep_active_null_expiry(runtime):
    runtime.profile = runtime.store.put_profile("knowledge-profile", resources=[runtime.resource],
        memory_policy=runtime.profile["memory_policy"], long_term_memory_days=None)
    runtime.task["profile_snapshot"]["long_term_memory_days"] = None
    args = arguments(runtime, [runtime.record()])
    first = update(runtime, args, runtime.task)
    assert first["data"]["expires_at"] is None
    assert first["data"]["memory_state"] == "active"
    second = replay(runtime, args, first)
    assert second["data"]["memory_state"] == "active"
    assert second["data"]["expires_at"] is None
    assert runtime.memory.get(runtime.profile["profile_id"], second["data"]["memory_ref"])["expires_at"] is None


@pytest.mark.parametrize("retention", ["source_bound", "long_term"])
def test_memory_update_accepts_unbounded_task_and_observation_without_losing_sources(runtime, retention):
    runtime.profile = runtime.store.put_profile("knowledge-profile", resources=[runtime.resource],
        memory_policy=runtime.profile["memory_policy"], retention_days=None, long_term_memory_days=None)
    runtime.task["profile_snapshot"].update(retention_days=None, long_term_memory_days=None)
    runtime.plan["draft"]["output"]["resource_rules"][0]["memory"]["retention"] = retention
    reference = runtime.record(expires_at=None)
    args = arguments(runtime, [reference])
    first = update(runtime, args, runtime.task)
    assert first["data"]["expires_at"] is None
    item = runtime.memory.get(runtime.profile["profile_id"], first["data"]["memory_ref"])
    assert item["retention"] == retention
    assert item["sources"][0]["record_ref"] == reference
    assert replay(runtime, args, first)["data"]["memory_state"] == "active"
