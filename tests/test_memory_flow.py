import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from daeri.engine import Engine
from daeri.resources import ResourceRegistry
from daeri.storage import Store
from test_execution_flow import ROOT, approve, begin, comparison_report, execution, resource, table_plan


@pytest.fixture
def remembered(tmp_path, request):
    options = getattr(request, "param", {})
    store = Store(tmp_path)
    resources = [resource(), resource("cache-b", "7.00")]
    for item in resources:
        item["policy"]["memory"] = {"categories": ["procedure"], "source_fields": [ROOT],
            "context_sources": ["request", "clarification"], "retention": options.get("memory_retention", "long_term"), "allow_model_recall": True}
    targets = {item["instance_id"]: hashlib.sha256(json.dumps(item["target"], sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() for item in resources}
    profile = store.put_profile("remembered-service", resources=resources, retention_days=options.get("retention_days"), memory_policy={
        "categories": ["procedure"], "instance_ids": list(targets), "target_bindings": targets,
        "model_allowed": True, "retain_allowed": True, "long_term_allowed": True, "exports_allowed": True})
    registry = ResourceRegistry(store)
    for item in resources:
        registry.diagnose(profile, item["instance_id"])
    store.close()
    engine = Engine(tmp_path)
    try:
        saved, draft = begin(engine, profile, configure=table_plan)
        approved = approve(engine, saved)
        first = engine.call("daeri_capability_execute", execution(saved, approved, draft["steps"][0]))
        assert first["status"] == "ok", first
        second = engine.call("daeri_capability_execute", execution(saved, {**approved, "task_revision": first["data"]["task_revision"]}, draft["steps"][1], call_id="call-2"))
        assert second["status"] == "ok", second
        report = engine.call("daeri_result_submit", {"request_id": "report", "task_id": saved["task_id"],
            "expected_task_revision": second["data"]["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"],
            "report": comparison_report(first["data"], second["data"])})
        assert report["status"] == "ok", report
        yield engine, profile, saved, draft, approved, first["data"], second["data"], report["data"]
    finally:
        engine.close()


def knowledge_args(data):
    engine, profile, saved, draft, approved, first, second, report = data
    return {"request_id": "remember", "task_id": saved["task_id"], "expected_task_revision": report["task_revision"],
        "operation": "store", "source_refs": [report["report_ref"]], "reason": "다음 비교 업무에서 확인한 절차를 재사용",
        "draft": {"category": "procedure", "statement": "Cache 비교 작업은 두 리소스의 동일 식별자 필드를 대조하고 차이를 검산한다",
            "applicability": "현재 profile의 Cache 결과 비교", "instance_ids": ["cache-a", "cache-b"],
            "verification_method": "두 하위 MCP의 실제 결과와 검증표·CSV 생성 결과를 대조"}}


def new_task(engine, profile, key="next"):
    result = engine.call("daeri_task_create", {"request_id": key, "request_text": "이전과 비슷한 Cache 비교를 수행해줘", "context": {"profile_id": profile["profile_id"]}})
    assert result["status"] == "ok", result
    return result["data"]


def test_actual_tasks_report_memory_update_retrieval_and_new_plan_still_requires_approval(remembered):
    engine, profile, saved, draft, approved, first, second, report = remembered
    args = knowledge_args(remembered)
    update = engine.call("daeri_memory_update", args)
    assert update["status"] == "partial", update
    assert update["error"]["code"] == "MEMORY_INDEX_INCOMPLETE"
    item = update["data"]
    assert item["memory_state"] == "active" and item["index_state"] == "pending"
    assert item["expires_at"] is None
    task_expiry = engine.store._db.execute("SELECT expires_at FROM tasks WHERE task_id=?", (saved["task_id"],)).fetchone()[0]
    assert task_expiry is None
    repeated = engine.call("daeri_memory_update", args)
    assert repeated["data"] == item
    assert engine.store.get_task(saved["task_id"])["state"] == "succeeded"
    task = new_task(engine, profile)
    search = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache 비교"})
    assert search["status"] == "partial", search
    automatic = {entry['memory_ref'] for entry in report['memory']['items'] if entry.get('memory_ref')}
    assert {entry['memory_ref'] for entry in search['data']['items']} == automatic | {item['memory_ref']}
    planned = copy.deepcopy(draft)
    planned.update(memory_search_refs=[search["data"]["memory_search_ref"]], memory_refs=[item["memory_ref"]], memory_limitations=[])
    next_plan = engine.call("daeri_plan_submit", {"request_id": "plan-next", "task_id": task["task_id"], "expected_task_revision": task["task_revision"], "draft": planned})
    assert next_plan["status"] == "ok", next_plan
    attempted = engine.call("daeri_capability_execute", execution(next_plan["data"], {**approved, "task_revision": next_plan["data"]["task_revision"]}, planned["steps"][0], call_id="without-new-approval"))
    assert attempted["status"] == "error"
    assert engine.store.get_task(task["task_id"])["calls"] == []


def test_long_term_knowledge_remains_without_recreating_expired_source_bodies(remembered):
    engine, profile, saved, draft, approved, first, second, report = remembered
    stored = engine.call("daeri_memory_update", knowledge_args(remembered))
    assert stored["status"] == "partial", stored
    reference = stored["data"]["memory_ref"]
    expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    engine.store.set_task_expiry(saved["task_id"], expired)
    engine.store.cleanup()
    task = new_task(engine, profile)
    found = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache 비교"})
    assert found["status"] == "partial", found
    assert found["data"]["items"][0]["memory_ref"] == reference
    assert found["data"]["items"][0]["sources"] == []
    assert found["data"]["limitations"]
    assert engine.memory.can_export(profile["profile_id"], reference) is True
    old = engine.call("daeri_result_get", {"task_id": saved["task_id"], "record_ref": first["result_ref"]})
    assert old["error"]["code"] == "RECORD_EXPIRED"
    assert engine.store._db.execute("SELECT count(*) FROM records WHERE task_id=? AND cleaned=0", (saved["task_id"],)).fetchone()[0] == 0


def test_verified_artifact_resolves_to_original_observations_for_memory(remembered):
    engine, profile, saved, draft, approved, first, second, report = remembered
    args = knowledge_args(remembered)
    args["source_refs"] = [report["artifact_refs"][0]]
    result = engine.call("daeri_memory_update", args)
    assert result["status"] == "partial", result
    item = engine.memory.get(profile["profile_id"], result["data"]["memory_ref"])
    assert {source["record_ref"] for source in item["sources"]} == {first["result_ref"], second["result_ref"]}


def test_exportable_past_memory_can_be_included_in_current_report_file(remembered):
    engine, profile, original, draft, previous, first, second, report = remembered
    update = engine.call("daeri_memory_update", knowledge_args(remembered))
    reference = update["data"]["memory_ref"]
    assert engine.memory.can_export(profile["profile_id"], reference)
    task = new_task(engine, profile, "memory-export-task")
    search = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache 비교"})
    selected = copy.deepcopy(draft)
    selected.update(memory_search_refs=[search["data"]["memory_search_ref"]], memory_refs=[reference], memory_limitations=[])
    selected["output"]["artifact_rules"] = [{"artifact_id": "report-text", "format_id": "text",
        "format_contract_ref": "format_text_v1", "input_kind": "report", "options": {}, "max_bytes": 10000}]
    saved = engine.call("daeri_plan_submit", {"request_id": "memory-plan", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"], "draft": selected})["data"]
    approval = approve(engine, saved)
    a = engine.call("daeri_capability_execute", execution(saved, approval, selected["steps"][0], call_id="export-a"))["data"]
    b = engine.call("daeri_capability_execute", execution(saved, {**approval, "task_revision": a["task_revision"]}, selected["steps"][1], call_id="export-b"))["data"]
    body = comparison_report(a, b)
    body["findings"].append({"kind": "memory", "text": "과거 확인한 절차를 참고했다", "evidence_refs": [reference]})
    body["artifacts"] = [{"kind": "report", "artifact_id": "report-text", "label": "기억을 참고한 보고"}]
    result = engine.call("daeri_result_submit", {"request_id": "memory-report", "task_id": task["task_id"],
        "expected_task_revision": b["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "report": body})
    assert result["status"] == "ok", result
    artifact = engine.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": result["data"]["artifact_refs"][0]})
    assert artifact["status"] == "ok", artifact
    assert artifact["data"]["managed_file"]["local_path"]


@pytest.mark.parametrize("restriction", ["empty", "partial", "redact", "omit"])
@pytest.mark.parametrize("delivery", ["create", "read_legacy"])
def test_current_plan_blocks_one_contributing_memory_source_in_report_file(remembered, restriction, delivery):
    engine, profile, original, draft, previous, first, second, report = remembered
    learned = engine.call("daeri_memory_update", knowledge_args(remembered))
    reference = learned["data"]["memory_ref"]
    assert engine.memory.can_export(profile["profile_id"], reference)
    task = new_task(engine, profile, "restricted-memory-export")
    search = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache 비교"})
    selected = copy.deepcopy(draft)
    selected.update(steps=[], memory_search_refs=[search["data"]["memory_search_ref"]],
        memory_refs=[reference], memory_limitations=[],
        completion_criteria=[{"criterion_id": "explain", "description": "과거 비교 절차 설명", "required": True, "step_ids": []}])
    selected["output"]["table_rules"] = []
    selected["output"]["artifact_rules"] = [{"artifact_id": "memory-report", "format_id": "text",
        "format_contract_ref": "format_text_v1", "input_kind": "report", "options": {}, "max_bytes": 10000}]
    rule = selected["output"]["resource_rules"][1]
    field = {"segments": [{"key": "data"}, {"key": "rows"}, {"each": True}, {"key": "id"}]}
    if restriction in {"empty", "partial"}:
        rule["export_fields"] = [] if restriction == "empty" else [field]
    else:
        rule["masking"] = [{"field": field, "action": restriction}]
    saved = engine.call("daeri_plan_submit", {"request_id": "restricted-plan", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"], "draft": selected})
    assert saved["status"] == "ok", saved
    approval = approve(engine, saved["data"])
    if delivery == "read_legacy":
        # Earlier runtimes could register this file despite the plan's tighter source policy.
        reference_file = engine.store.write_artifact(task["task_id"], b"previously exported memory", "text", "text/plain", "txt")
        current = engine.store.get_task(task["task_id"])
        revision = current["task_revision"]
        current["artifact_ids"] = {"memory-report": {"artifact_ref": reference_file, "source_refs": [reference]}}
        current["artifact_permissions"] = {reference_file: {"plan_id": saved["data"]["plan_id"]}}
        current["task_revision"] += 1
        engine.store.update_task(current, revision)
        fetched = engine.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": reference_file})
        assert fetched["status"] == "error", fetched
        assert fetched["error"]["code"] == "ACCESS_DENIED"
        assert fetched["data"] is None
        return
    body = {"summary": "과거 확인한 Cache 비교 절차", "proposed_task_state": "succeeded",
        "findings": [{"kind": "memory", "text": "두 리소스의 동일 식별자를 대조한다", "evidence_refs": [reference]}],
        "tables": [], "checks": [{"criterion_id": "explain", "status": "met", "method": "과거 절차 근거 확인", "evidence_refs": [reference]}],
        "limitations": [], "uncertain_effects": [], "next_actions": [],
        "artifacts": [{"kind": "report", "artifact_id": "memory-report", "label": "과거 절차 보고"}]}
    before = engine.store.list_records(task["task_id"])
    submitted = engine.call("daeri_result_submit", {"request_id": "restricted-report", "task_id": task["task_id"],
        "expected_task_revision": approval["task_revision"], "plan_id": saved["data"]["plan_id"],
        "plan_version": saved["data"]["plan_version"], "report": body})
    assert submitted["status"] == "error", submitted
    assert submitted["error"]["code"] == "ACCESS_DENIED"
    assert engine.store.list_records(task["task_id"]) == before


@pytest.mark.parametrize("remembered", [{"memory_retention": "source_bound"}], indirect=True)
def test_actual_source_bound_memory_and_artifact_share_unbounded_original_retention(remembered):
    engine, profile, saved, draft, approved, first, second, report = remembered
    update = engine.call("daeri_memory_update", knowledge_args(remembered))
    assert update["status"] == "partial", update
    assert update["data"]["expires_at"] is None
    item = engine.memory.get(profile["profile_id"], update["data"]["memory_ref"])
    assert item["retention"] == "source_bound"
    assert len(item["sources"]) == 2
    artifact = engine.call("daeri_result_get", {"task_id": saved["task_id"], "record_ref": report["artifact_refs"][0]})
    assert artifact["status"] == "ok", artifact
    assert artifact["data"]["managed_file"]["expires_at"] is None


@pytest.mark.parametrize("remembered", [{"memory_retention": "source_bound", "retention_days": 2}], indirect=True)
def test_actual_finite_profile_still_limits_observations_reports_files_and_memory(remembered):
    engine, profile, saved, draft, approved, first, second, report = remembered
    update = engine.call("daeri_memory_update", knowledge_args(remembered))
    assert update["status"] == "partial", update
    expiry = datetime.fromisoformat(update["data"]["expires_at"])
    assert datetime.now(UTC) + timedelta(days=1.99) < expiry <= datetime.now(UTC) + timedelta(days=2)
    for reference in (first["result_ref"], second["result_ref"], report["report_ref"], *report["artifact_refs"]):
        record = engine.store.read_record(saved["task_id"], reference)
        assert datetime.fromisoformat(record["expires_at"]) <= datetime.now(UTC) + timedelta(days=2)


@pytest.mark.parametrize("format_id", ["text", "json"])
def test_actual_engine_blocks_model_only_values_in_report_files(remembered, format_id):
    engine, profile, saved, original, approved, first, second, report = remembered
    task = new_task(engine, profile, "private-report-task")
    search = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "comparison"})
    draft = copy.deepcopy(original)
    draft.update(memory_search_refs=[search["data"]["memory_search_ref"]], memory_refs=[], memory_limitations=[])
    draft["output"]["table_rules"] = []
    for rule in draft["output"]["resource_rules"]:
        rule["export_fields"] = []
    draft["output"]["artifact_rules"] = [{"artifact_id": "private-report", "format_id": format_id,
        "format_contract_ref": f"format_{format_id}_v1", "input_kind": "report", "options": {}, "max_bytes": 10000}]
    planned = engine.call("daeri_plan_submit", {"request_id": "private-plan", "task_id": task["task_id"], "expected_task_revision": task["task_revision"], "draft": draft})
    assert planned["status"] == "ok", planned
    permission = approve(engine, planned["data"])
    a = engine.call("daeri_capability_execute", execution(planned["data"], permission, draft["steps"][0], call_id="private-a"))["data"]
    b = engine.call("daeri_capability_execute", execution(planned["data"], {**permission, "task_revision": a["task_revision"]}, draft["steps"][1], call_id="private-b"))["data"]
    private_report = {"summary": "private 10.00", "proposed_task_state": "succeeded",
        "findings": [{"kind": "observation", "text": "private 10.00", "evidence_refs": [a["result_ref"]]}],
        "tables": [], "checks": [{"criterion_id": "read", "status": "met", "method": "실제 호출 결과 확인", "evidence_refs": [a["result_ref"], b["result_ref"]]}],
        "limitations": [], "uncertain_effects": [], "next_actions": [],
        "artifacts": [{"kind": "report", "artifact_id": "private-report", "label": "모델만 볼 수 있는 결과"}]}
    before = engine.store.list_records(task["task_id"])
    submitted = engine.call("daeri_result_submit", {"request_id": "private-report", "task_id": task["task_id"],
        "expected_task_revision": b["task_revision"], "plan_id": planned["data"]["plan_id"], "plan_version": planned["data"]["plan_version"], "report": private_report})
    assert submitted["status"] == "error", submitted
    assert submitted["error"]["code"] == "RESULT_VALIDATION_FAILED"
    assert engine.store.list_records(task["task_id"]) == before
    private_report["artifacts"] = []
    preserved = engine.call("daeri_result_submit", {"request_id": "model-only-report", "task_id": task["task_id"],
        "expected_task_revision": b["task_revision"], "plan_id": planned["data"]["plan_id"],
        "plan_version": planned["data"]["plan_version"], "report": private_report})
    assert preserved["status"] == "ok", preserved
    learned = engine.call("daeri_memory_update", {"request_id": "model-only-memory", "task_id": task["task_id"],
        "expected_task_revision": preserved["data"]["task_revision"], "operation": "store", "source_refs": [preserved["data"]["report_ref"]],
        "reason": "실제 조회 절차만 재사용", "draft": {"category": "procedure", "statement": "Cache 조회는 승인한 profile의 각 리소스에서 정해진 키를 읽는다",
            "applicability": "현재 profile의 Cache 조회", "instance_ids": ["cache-a", "cache-b"], "verification_method": "실제 승인한 두 조회 호출과 대조"}})
    assert learned["status"] == "partial", learned
    assert engine.memory.get(profile["profile_id"], learned["data"]["memory_ref"])["statement"]
    assert engine.memory.can_export(profile["profile_id"], learned["data"]["memory_ref"]) is False
