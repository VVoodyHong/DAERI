"""Scoped host subagents share one approval, execution ledger and profile memory."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import subprocess
import sys
import threading

import pytest

from daeri.application.runtime import Runtime
from test_execution_flow import configured, begin, approve, execution
from test_runtime import bundle_runtime, bundle_plan


def create_subagent(runtime, saved, step_ids, *, key="subagent", **extra):
    revision = runtime.store.get_task(saved["task_id"])["task_revision"]
    return runtime.call("daeri_subagent", {
        "operation": "create", "request_id": key, "task_id": saved["task_id"],
        "expected_task_revision": revision,
        "assignment": {"label": "조회 하위 에이전트", "objective": "승인된 조회를 확인한다",
            "step_ids": step_ids, "completion_criteria": ["출처와 확인 한계를 보고한다"], "source_refs": []},
        **extra,
    })


def subagent_call(runtime, saved, grant, operation, **extra):
    return runtime.call("daeri_subagent", {"operation": operation, "task_id": saved["task_id"],
        "delegation_token": grant["delegation_token"], **extra})


def run_read(runtime, saved, grant, step="read-0", call_id="subagent-read", **extra):
    return subagent_call(runtime, saved, grant, "execute", call={
        "step_id": step, "call_id": call_id, "parameters": {"key": "orders"}}, **extra)


@pytest.mark.parametrize('content', ['# 조사 결과\n자유 서술', {'table': [[1, 2]], 'evidence_refs': ['본문 데이터']}, None])
def test_subagent_preserves_free_content_and_resolves_its_actual_sources(configured, content):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ['read-0'])['data']
    observed = run_read(runtime, saved, grant)['data']
    completed = subagent_call(runtime, saved, grant, 'complete', result=content)
    assert completed['status'] == 'ok', completed
    assert completed['data']['result'] == {'content': content, 'evidence_refs': [observed['result_ref']]}
    repeated = subagent_call(runtime, saved, grant, 'complete', result=content)
    assert repeated['data']['result_ref'] == completed['data']['result_ref']
    changed = subagent_call(runtime, saved, grant, 'complete', result='다른 결과')
    assert changed['error']['code'] == 'SUBAGENT_CONFLICT'


def test_subagent_explicit_result_sources_stay_inside_the_assignment(configured):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    other = runtime.call('daeri_capability_execute', execution(saved, approved, draft['steps'][1]))['data']
    grant = create_subagent(runtime, saved, ['read-0'])['data']
    denied = subagent_call(runtime, saved, grant, 'complete', result='분석', evidence_refs=[other['result_ref']])
    assert denied['error']['code'] == 'ACCESS_DENIED'


def test_subagent_creation_requires_real_orchestrator_approval_and_registered_steps(configured):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    assert create_subagent(runtime, saved, ["read-0"])["error"]["code"] == "APPROVAL_REQUIRED"
    approve(runtime, saved)
    assert create_subagent(runtime, saved, ["invented"])["error"]["code"] == "SCOPE_VIOLATION"
    grant = create_subagent(runtime, saved, ["read-0"])
    assert grant["status"] == "ok", grant
    assert [step["step_id"] for step in grant["data"]["steps"]] == ["read-0"]
    assert len(grant["data"]["contracts"]) == 1


def test_subagent_on_another_runtime_executes_only_assigned_step_and_replays(configured):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    subagent = Runtime(runtime.store.data_dir)
    try:
        denied = run_read(subagent, saved, grant, step="read-1")
        assert denied["error"]["code"] == "SCOPE_VIOLATION"
        first = run_read(subagent, saved, grant)
        assert first["status"] == "ok", first
        assert first["data"]["inline_result"]["data"]["rows"][0]["amount"] == "10.00"
        assert run_read(subagent, saved, grant)["data"]["result_ref"] == first["data"]["result_ref"]
        assert runtime.store.get_task(saved["task_id"])["usage"]["tool_calls"] == 1
        grant["delegation_token"] = "invented"
        assert run_read(subagent, saved, grant)["error"]["code"] == "ACCESS_DENIED"
    finally:
        subagent.close()


def test_subagents_and_orchestrator_can_share_an_approved_read_step(configured):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    create_subagent(runtime, saved, ["read-0"])
    assert create_subagent(runtime, saved, ["read-0"], key="overlap")["status"] == "ok"
    args = execution(saved, approved, draft["steps"][0])
    args["expected_task_revision"] = runtime.store.get_task(saved["task_id"])["task_revision"]
    assert runtime.call("daeri_capability_execute", args)["status"] == "ok"


def test_parallel_subagents_spend_one_shared_budget_without_revision_races(configured):
    runtime, profile = configured
    saved, _ = begin(runtime, profile, max_calls=2, max_runtime=30000)
    approve(runtime, saved)
    a = create_subagent(runtime, saved, ["read-0"], key="a")["data"]
    b = create_subagent(runtime, saved, ["read-1"], key="b")["data"]
    subagents = [Runtime(runtime.store.data_dir), Runtime(runtime.store.data_dir)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run_read, subagents[0], saved, a, "read-0", "a-read"),
                       pool.submit(run_read, subagents[1], saved, b, "read-1", "b-read")]
            outcomes = [future.result(timeout=15) for future in futures]
        assert [result["status"] for result in outcomes] == ["ok", "ok"], outcomes
        task = runtime.store.get_task(saved["task_id"])
        assert task["usage"]["tool_calls"] == 2
        assert task["reserved"]["runtime_ms"] == 0
        assert run_read(subagents[0], saved, a, call_id="extra")["error"]["code"] == "BUDGET_EXCEEDED"
    finally:
        for subagent in subagents:
            subagent.close()


def test_subagent_reads_only_assigned_or_own_evidence_and_completes_with_provenance(configured):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    other = runtime.call("daeri_capability_execute", execution(saved, approved, draft["steps"][1]))["data"]
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    own = run_read(runtime, saved, grant)["data"]
    assert subagent_call(runtime, saved, grant, "read", record_ref=other["result_ref"])["error"]["code"] == "ACCESS_DENIED"
    assert subagent_call(runtime, saved, grant, "read", record_ref=own["result_ref"])["status"] == "ok"
    result = '조회 완료'
    completed = subagent_call(runtime, saved, grant, "complete", request_id="done", result=result,
        evidence_refs=[own['result_ref']])
    assert completed["status"] == "ok", completed
    assert completed["data"]["state"] == "completed"
    assert completed["data"]["result"] == {'content': result, 'evidence_refs': [own['result_ref']]}
    loaded = runtime.call("daeri_result_get", {"task_id": saved["task_id"],
        "record_ref": completed["data"]["result_ref"]})
    assert loaded["data"]["payload"] == {'content': result, 'evidence_refs': [own['result_ref']]}
    assert subagent_call(runtime, saved, grant, "complete", request_id="done", result=result,
        evidence_refs=[own['result_ref']])["data"]["result_ref"] == completed["data"]["result_ref"]
    assert run_read(runtime, saved, grant, call_id="after-complete")["error"]["code"] == "SUBAGENT_UNAVAILABLE"
    task = runtime.store.get_task(saved["task_id"])
    assert task["state"] == "running"
    assert task["last_report_ref"] is None


def test_orchestrator_can_recover_subagent_metadata_without_exposing_tokens(configured):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    fetched = runtime.call("daeri_task_get", {"task_id": saved["task_id"]})["data"]["task"]
    assert fetched["subagents"][0]["delegation_id"] == grant["delegation_id"]
    assert fetched["subagents"][0]["step_ids"] == ["read-0"]
    assert "delegation_token" not in fetched["subagents"][0]
    assert "token_hash" not in fetched["subagents"][0]


def test_explicit_source_handoff_can_cross_assigned_execution_instances(configured):
    runtime, profile = configured
    saved, draft = begin(runtime, profile)
    approved = approve(runtime, saved)
    prior = runtime.call("daeri_capability_execute", execution(saved, approved, draft["steps"][1]))["data"]
    args = {"operation": "create", "request_id": "handoff", "task_id": saved["task_id"],
        "expected_task_revision": prior["task_revision"], "assignment": {"label": "비교", "objective": "이전 근거와 비교",
            "step_ids": ["read-0"], "completion_criteria": ["비교 근거 반환"], "source_refs": [prior["result_ref"]]}}
    grant = runtime.call("daeri_subagent", args)
    assert grant["status"] == "ok", grant
    assert subagent_call(runtime, saved, grant["data"], "read", record_ref=prior["result_ref"])["status"] == "ok"
    assert run_read(runtime, saved, grant["data"], step="read-1")["error"]["code"] == "SCOPE_VIOLATION"


def test_subagent_summary_cannot_restore_source_fields_removed_at_task_end(configured):
    runtime, profile = configured
    def transient(draft):
        draft["output"]["resource_rules"][0]["retained_fields"] = []
    saved, draft = begin(runtime, profile, configure=transient)
    approved = approve(runtime, saved)
    other = runtime.call("daeri_capability_execute", execution(saved, approved, draft["steps"][1]))["data"]
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    own = run_read(runtime, saved, grant)["data"]
    completed = subagent_call(runtime, saved, grant, "complete", request_id="done",
        result='조회 값은 10.00이다', evidence_refs=[own['result_ref']])["data"]
    task = runtime.store.get_task(saved["task_id"])
    assert runtime.call("daeri_result_submit", {"request_id": "report", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"], **grant["plan_ref"], "report": '두 조회 완료',
        'evidence_refs': [own['result_ref'], other['result_ref']]})["status"] == "ok"
    loaded = runtime.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": completed["result_ref"]})
    assert loaded["error"]["code"] == "ACCESS_DENIED"


@pytest.mark.parametrize("dispatched", [True, False])
@pytest.mark.parametrize("terminal", [False, True])
def test_orchestrator_resume_recovers_a_dead_subagent_without_losing_shared_accounting(configured, dispatched, terminal):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    child = """
import json, os, sys
from daeri.application.runtime import Runtime
p = json.load(sys.stdin)
runtime = Runtime(p['directory'])
if p['dispatched']:
    runtime.registry.execute = lambda *a, **kw: os._exit(0)
else:
    original = runtime._execution_allowed
    checks = 0
    def allowed(*a, **kw):
        global checks
        checks += 1
        if checks == 2:
            os._exit(0)
        return original(*a, **kw)
    runtime._execution_allowed = allowed
runtime.call('daeri_subagent', {'operation':'execute', 'task_id':p['task_id'], 'delegation_token':p['token'],
    'call':{'step_id':'read-0', 'call_id':'crashed', 'parameters':{'key':'orders'}}})
"""
    crashed = subprocess.run([sys.executable, "-c", child], input=json.dumps({
        "directory": str(runtime.store.data_dir), "task_id": saved["task_id"],
        "token": grant["delegation_token"], "dispatched": dispatched}), text=True, capture_output=True, timeout=10)
    assert crashed.returncode == 0, crashed.stderr
    task = runtime.store.get_task(saved["task_id"])
    assert task["calls"][0]["call_state"] == "pending"
    if terminal:
        ended = runtime.call("daeri_result_submit", {"request_id": "partial", "task_id": task["task_id"],
            "expected_task_revision": task["task_revision"], **grant["plan_ref"], "report": '하위 에이전트 중단',
            'state': 'partial'})
        assert ended["status"] == "ok", ended
        task = runtime.store.get_task(saved["task_id"])
        assert task["state"] == "partial"
    result = runtime.call("daeri_task_resume", {"request_id": "recover-subagent", "task_id": saved["task_id"],
        "expected_task_revision": task["task_revision"]})
    assert result["status"] in {"ok", "pending"}, result
    task = runtime.store.get_task(saved["task_id"])
    assert task["calls"][0]["call_state"] == ("failed" if dispatched else "blocked")
    assert task["reserved"]["runtime_ms"] == 0
    assert task["usage"]["tool_calls"] == int(dispatched)


@pytest.mark.parametrize("action", ["pause", "cancel", "revoke", "expire"])
def test_subagent_authority_ends_with_orchestrator_control_or_expiry(configured, action):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    task = runtime.store.get_task(saved["task_id"])
    if action == "pause":
        runtime.call("daeri_task_update", {"request_id": "pause", "task_id": saved["task_id"],
            "expected_task_revision": task["task_revision"], "action": "pause"})
    elif action == "cancel":
        runtime.call("daeri_task_cancel", {"request_id": "cancel", "task_id": saved["task_id"],
            "expected_task_revision": task["task_revision"]})
    elif action == "revoke":
        result = runtime.call("daeri_subagent", {"operation": "revoke", "request_id": "revoke",
            "task_id": saved["task_id"], "expected_task_revision": task["task_revision"],
            "delegation_id": grant["delegation_id"]})
        assert result["data"]["state"] == "revoked"
    else:
        with runtime.store.transaction():
            task["delegations"][0]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            runtime._save(task)
    assert run_read(runtime, saved, grant)["error"]["code"] == "SUBAGENT_UNAVAILABLE"
    assert runtime.store.get_task(saved["task_id"])["calls"] == []


def test_subagent_search_uses_shared_profile_memory_but_scopes_instances(configured):
    runtime, profile = configured
    profile = runtime.store.put_profile("subagent-search", resources=profile["resources"],
        core_context=["운영 용어"], embedding={"provider": "keyword_only"}, memory_policy={"categories": ["procedure"],
            "instance_ids": ["cache-a"], "model_allowed": True, "retain_allowed": True})
    for resource in profile["resources"]:
        runtime.registry.diagnose(profile, resource["instance_id"])
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    result = subagent_call(runtime, saved, grant, "search", query="집계 방법")
    assert result["status"] in {"ok", "partial"}, result
    assert result["data"]["core_context"] == ["운영 용어"]
    denied = subagent_call(runtime, saved, grant, "search", query="집계 방법", instance_ids=["cache-b"])
    assert denied["error"]["code"] == "SCOPE_VIOLATION"


def test_revoke_during_io_preserves_late_result_and_releases_shared_reservation(configured, monkeypatch):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    subagent = Runtime(runtime.store.data_dir)
    started, release = threading.Event(), threading.Event()
    real_execute = subagent.registry.execute
    def waiting(invocation, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return real_execute(invocation, **kwargs)
    monkeypatch.setattr(subagent.registry, "execute", waiting)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_read, subagent, saved, grant)
            assert started.wait(timeout=5)
            task = runtime.store.get_task(saved["task_id"])
            revoked = runtime.call("daeri_subagent", {"operation": "revoke", "request_id": "stop-subagent",
                "task_id": task["task_id"], "expected_task_revision": task["task_revision"],
                "delegation_id": grant["delegation_id"]})
            assert revoked["status"] == "ok", revoked
            assert create_subagent(runtime, saved, ["read-0"], key="replacement-during-read")["status"] == "ok"
            release.set()
            assert future.result(timeout=10)["error"]["code"] == "SUBAGENT_UNAVAILABLE"
        task = runtime.store.get_task(saved["task_id"])
        assert task["calls"][0]["call_state"] == "completed"
        assert task["calls"][0]["result_ref"]
        assert task["reserved"]["runtime_ms"] == 0
        assert task["usage"]["tool_calls"] == 1
        assert create_subagent(runtime, saved, ["read-0"], key="replacement")["status"] == "ok"
    finally:
        release.set()
        subagent.close()


@pytest.mark.parametrize("extra", [{"expected_task_revision": 1}, {"assignment": {}}, {"operation": "approve"}])
def test_subagent_gateway_rejects_mixed_operations(configured, extra):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    payload = {"operation": "get", "task_id": saved["task_id"], "delegation_token": "token", **extra}
    assert runtime.call("daeri_subagent", payload)["error"]["code"] == "INVALID_INPUT"


def test_subagent_selects_different_read_tools_in_one_approved_resource_step(bundle_runtime):
    runtime, profile = bundle_runtime
    saved, _ = bundle_plan(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    contracts = {item["capability_id"]: item for item in grant["contracts"]}
    assert set(contracts) == {"*", "get", "scan_keys"}
    assert contracts["scan_keys"]["input_schema"] == {"type": "object", "additionalProperties": True}
    refs = []
    for index, (capability, parameters) in enumerate([("get", {"key": "orders"}), ("scan_keys", {"pattern": "orders:*"})]):
        result = subagent_call(runtime, saved, grant, "execute", call={"step_id": "read-0", "call_id": f"chosen-{index}",
            "capability_id": capability, "parameters": parameters})
        assert result["status"] == "ok", result
        assert result["data"]["inline_result"]["data"]["tool"] == capability
        assert result["data"]["actual_capability_id"] == capability
        refs.append(result["data"]["result_ref"])
    completed = subagent_call(runtime, saved, grant, "complete", request_id="finished",
        result='필요한 도구를 선택해 조회했다', evidence_refs=refs)
    assert completed["status"] == "ok", completed
    assert runtime.store.get_task(saved["task_id"])["usage"]["tool_calls"] == 2


def test_subagent_bundle_requires_a_real_tool_selection_before_dispatch(bundle_runtime):
    runtime, profile = bundle_runtime
    saved, _ = bundle_plan(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ["read-0"])["data"]
    result = run_read(runtime, saved, grant)
    assert result["error"]["code"] == "INVALID_INPUT", result
    assert runtime.store.get_task(saved["task_id"])["calls"] == []


def test_subagent_uses_job_inputs_without_revision_keys_or_default_expiry(configured):
    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = runtime.call("daeri_subagent", {"operation": "create", "task_id": saved["task_id"],
        "assignment": {"label": "조회", "objective": "승인된 자료 조사", "step_ids": ["read-0"],
            "completion_criteria": ["실제 출처 반환"]}})
    assert grant["status"] == "ok", grant
    assert grant["data"]["expires_at"] is None
    observed = subagent_call(runtime, saved, grant["data"], "execute",
        call={"step_id": "read-0", "parameters": {"key": "orders"}})
    assert observed["status"] == "ok", observed
    finished = subagent_call(runtime, saved, grant["data"], "complete", result='조회 완료')
    repeated = subagent_call(runtime, saved, grant["data"], "complete", result='조회 완료')
    assert finished["status"] == repeated["status"] == "ok", (finished, repeated)
    assert finished["data"]["result_ref"] == repeated["data"]["result_ref"]


def test_subagent_can_return_memory_it_discovers_during_approved_work(tmp_path):
    from contextlib import closing
    from test_automatic_memory import completed
    from test_simple_workflow import simple_plan

    with closing(Runtime(tmp_path)) as runtime:
        profile, original = completed(runtime)
        published = runtime.call('daeri_result_submit', original)
        reference = published['data']['memory']['items'][0]['memory_ref']
        task = runtime.call('daeri_task_create', {'request_text': 'Cache get',
            'context': {'profile_id': profile['profile_id']}})['data']
        saved = runtime.call('daeri_plan_submit', {'task_id': task['task_id'],
            'draft': simple_plan(profile)})['data']
        assert runtime.call('daeri_approval_request', {'task_id': task['task_id'],
            'plan_id': saved['plan_id'], 'decision': 'approve', 'user_response': '진행해'})['status'] == 'ok'
        grant = runtime.call('daeri_subagent', {'operation': 'create', 'task_id': task['task_id'],
            'assignment': {'label': '기억 활용', 'objective': '관련 절차 확인', 'step_ids': ['read-0', 'read-1'],
                'completion_criteria': ['기억의 실제 출처 반환']}})['data']
        found = subagent_call(runtime, saved, grant, 'search', query='Cache get')
        assert reference in {item['memory_ref'] for item in found['data']['items']}
        returned = subagent_call(runtime, saved, grant, 'complete',
            result='현재 profile의 관련 경험을 확인했다', evidence_refs=[reference])
        assert returned['status'] == 'ok', returned
        loaded = runtime.call('daeri_result_get', {'task_id': task['task_id'],
            'record_ref': returned['data']['result_ref']})
        assert loaded['status'] == 'ok', loaded
        assert loaded['data']['payload']['evidence_refs'] == [reference]


def test_subagent_result_can_supply_the_provenance_for_a_host_file(configured):
    from pathlib import Path

    runtime, profile = configured
    saved, _ = begin(runtime, profile)
    approve(runtime, saved)
    grant = create_subagent(runtime, saved, ['read-0', 'read-1'])['data']
    first = run_read(runtime, saved, grant, call_id='first')['data']
    second = run_read(runtime, saved, grant, step='read-1', call_id='second')['data']
    completed = subagent_call(runtime, saved, grant, 'complete', result='차이는 3이다',
        evidence_refs=[first['result_ref'], second['result_ref']])['data']
    path = Path(runtime.store.artifact_workspace(saved['task_id'])) / 'answer.txt'
    path.write_text('차이는 3이다')
    artifacts = [{'artifact_id': 'answer', 'label': '결과', 'local_path': str(path),
        'media_type': 'text/plain', 'source_refs': [completed['result_ref']]}]
    result = runtime.call('daeri_result_submit', {'task_id': saved['task_id'], 'report': '차이는 3이다',
        'evidence_refs': [completed['result_ref']], 'artifacts': artifacts})
    assert result['status'] == 'ok', result
    task = runtime.store.get_task(saved['task_id'])
    assert set(task['artifact_ids']['answer']['source_refs']) == {first['result_ref'], second['result_ref']}
