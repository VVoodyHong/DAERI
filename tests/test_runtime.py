from datetime import UTC, datetime, timedelta

import pytest

from daeri.application.runtime import Runtime
from daeri.infrastructure.storage import Store
from test_execution_flow import configured, begin, approve, execution, resource


@pytest.mark.parametrize("code,action", [
    ("CALL_ID_CONFLICT", "stop"), ("CONTEXT_CHANGED", "stop"),
    ("AUTHENTICATION_REQUIRED", "stop"), ("RECORD_EXPIRED", "stop"),
    ("CONTRACT_CHANGED", "refresh"), ("MEMORY_SEARCH_INCOMPLETE", "refresh"),
    ("APPROVAL_REQUIRED", "request_approval"), ("BUDGET_EXCEEDED", "request_approval"),
    ("READ_INTERRUPTED", "refresh"), ("INTENT_PERSIST_FAILED", "retry_same_request"),
])
def test_errors_direct_the_host_to_the_contracted_recovery_action(code, action):
    from daeri.application.runtime import response
    assert response(None, "error", code)["error"]["next_action"] == action


@pytest.fixture
def configured_runtime(tmp_path):
    store = Store(tmp_path)
    profile = store.put_profile("service-prod", core_context=["UTC 기준 운영"])
    store.close()
    runtime = Runtime(tmp_path)
    yield runtime, profile
    runtime.close()


def create(runtime, profile, key="create-1", text="운영 절차를 설명해줘"):
    result = runtime.call("daeri_task_create", {
        "request_id": key, "request_text": text,
        "context": {"profile_id": profile["profile_id"]},
    })
    assert result["status"] == "ok", result
    return result["data"]


@pytest.mark.parametrize("source_count,accepted", [(255, True), (256, False)])
def test_derived_result_reads_count_the_root_in_the_source_graph_limit(configured_runtime, source_count, accepted):
    runtime, profile = configured_runtime
    task = create(runtime, profile)
    with runtime.store.transaction():
        sources = [runtime.store.write_record(task["task_id"], "local_check", {"outcome": "passed"})
                   for _ in range(source_count)]
        ref = runtime.store.write_record(task["task_id"], "report", {"evidence_refs": sources})
    result = runtime.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": ref})
    assert result["status"] == ("ok" if accepted else "error"), result
    if not accepted:
        assert result["error"]["code"] == "RESULT_VALIDATION_FAILED"


@pytest.mark.parametrize("intermediate_count,accepted", [(29, True), (30, False)])
def test_derived_result_reads_check_shared_sources_at_the_deeper_path(configured_runtime, monkeypatch, intermediate_count, accepted):
    from itertools import count
    from uuid import UUID

    runtime, profile = configured_runtime
    task = create(runtime, profile)
    # The shallow source sorts before the deeper branch on every run.
    identities = count(1)
    monkeypatch.setattr("daeri.infrastructure.storage.uuid4", lambda: UUID(int=next(identities)))
    with runtime.store.transaction():
        leaf = runtime.store.write_record(task["task_id"], "local_check", {"outcome": "passed"})
        root = shared = runtime.store.write_record(task["task_id"], "report", {"evidence_refs": [leaf]})
        for _ in range(intermediate_count):
            root = runtime.store.write_record(task["task_id"], "report", {"evidence_refs": [root]})
        ref = runtime.store.write_record(task["task_id"], "report", {"evidence_refs": [shared, root]})
    result = runtime.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": ref})
    assert result["status"] == ("ok" if accepted else "error"), result
    if not accepted:
        assert result["error"]["code"] == "RESULT_VALIDATION_FAILED"


def draft(search_ref):
    return {
        "purpose": "현재 요청과 설정만 검토", "steps": [],
        "budget": {"max_tool_calls": 0, "max_external_requests": 0, "max_runtime_ms": 0, "resource_limits": []},
        "output": {"format": "text", "destination": "현재 대화", "resource_rules": []},
        "memory_search_refs": [search_ref], "memory_refs": [], "memory_limitations": [],
        "completion_criteria": [{"criterion_id": "explanation", "description": "요청 맥락 설명", "required": True, "step_ids": []}],
    }


def plan(runtime, task, key="plan-1"):
    search = runtime.call("daeri_memory_search", {"task_id": task["task_id"], "query": "운영 절차"})
    assert search["status"] == "partial"
    assert search["error"]["code"] == "MEMORY_SEARCH_INCOMPLETE"
    assert search["data"]["retrieval_state"] == "keyword_only"
    assert search["data"]["task_revision"] == task["task_revision"]
    args = {"request_id": key, "task_id": task["task_id"], "expected_task_revision": task["task_revision"], "draft": draft(search["data"]["memory_search_ref"])}
    result = runtime.call("daeri_plan_submit", args)
    assert result["status"] == "ok", result
    return result["data"]


def request_approval(runtime, saved, key="approval-1"):
    result = runtime.call("daeri_approval_request", {"request_id": key, "task_id": saved["task_id"], "expected_task_revision": saved["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "stage": "request"})
    assert result["status"] == "pending", result
    return result["data"]


def response_args(saved, waiting, key="response-1", decision="approve", text="이 계획대로 승인할게"):
    return {"request_id": key, "task_id": saved["task_id"], "expected_task_revision": waiting["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "stage": "respond", "approval_request_id": waiting["approval_request_id"], "decision": decision, "user_response": text}


@pytest.mark.parametrize("days", [None, 30])
def test_task_retention_is_explicit_in_plan_and_preserved_at_termination(configured_runtime, days):
    runtime, _ = configured_runtime
    profile = runtime.store.put_profile("retention", retention_days=days)
    task = create(runtime, profile)
    saved = plan(runtime, task)
    assert saved["retention_context"]["task_body_days"] == days
    cancelled = runtime.call("daeri_task_cancel", {"request_id": "cancel", "task_id": task["task_id"],
        "expected_task_revision": saved["task_revision"]})
    assert cancelled["status"] == "ok", cancelled
    current = runtime.store.get_task(task["task_id"])
    expiry = runtime.store.read_record(task["task_id"], current["request_ref"])["expires_at"]
    if days is None:
        assert expiry is None
    else:
        assert datetime.fromisoformat(expiry) == datetime.fromisoformat(current["finished_at"]) + timedelta(days=days)
    runtime.store.cleanup(now=datetime.now(UTC) + timedelta(days=3650))
    result = runtime.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": current["request_ref"]})
    assert result["status"] == ("ok" if days is None else "error"), result
    if days is not None:
        assert result["error"]["code"] == "RECORD_EXPIRED"


def test_task_survives_restart_without_request_body_in_sqlite(configured_runtime):
    runtime, profile = configured_runtime
    body = "UNIQUE_PRIVATE_REQUEST_BODY"
    task = create(runtime, profile, text=body)
    assert task["state"] == "planning"
    other = Runtime(runtime.store.data_dir)
    try:
        fetched = other.call("daeri_task_get", {"task_id": task["task_id"]})
        assert fetched["data"]["task"]["request_ref"] == task["request_ref"]
        assert fetched["data"]["task"]["execution_owner_status"] == "other"
    finally:
        other.close()
    for path in runtime.store.data_dir.glob("daeri.sqlite3*"):
        assert body.encode() not in path.read_bytes()


def test_create_replay_is_idempotent_and_different_input_conflicts(configured_runtime):
    runtime, profile = configured_runtime
    first = create(runtime, profile)
    again = create(runtime, profile)
    assert first == again
    conflict = runtime.call("daeri_task_create", {"request_id": "create-1", "request_text": "다른 업무", "context": {"profile_id": profile["profile_id"]}})
    assert conflict["error"]["code"] == "REQUEST_ID_CONFLICT"
    assert len(runtime.store.list_tasks(profile["profile_id"])) == 1


def test_missing_profile_waits_for_explicit_selection(configured_runtime):
    runtime, profile = configured_runtime
    task = runtime.call("daeri_task_create", {"request_id": "unresolved", "request_text": "상태 확인"})["data"]
    assert task["state"] == "awaiting_input"
    assert task["context"]["profile_id"] is None
    bound = runtime.call("daeri_task_update", {"request_id": "bind", "task_id": task["task_id"], "expected_task_revision": 1, "patch": {"context": {"profile_id": profile["profile_id"]}}})
    assert bound["status"] == "ok"
    assert bound["data"]["state"] == "planning"


def test_approval_requires_actual_response_and_replays_current_validity(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    waiting = request_approval(runtime, saved)
    missing = response_args(saved, waiting)
    missing.pop("user_response")
    assert runtime.call("daeri_approval_request", missing)["error"]["code"] == "INVALID_INPUT"
    args = response_args(saved, waiting)
    approved = runtime.call("daeri_approval_request", args)
    assert approved["status"] == "ok"
    assert approved["data"]["approval_id"]
    revision = approved["data"]["task_revision"]
    assert runtime.call("daeri_approval_request", args)["data"]["task_revision"] == revision
    paused = runtime.call("daeri_task_update", {"request_id": "pause", "task_id": saved["task_id"], "expected_task_revision": revision, "action": "pause"})
    assert paused["data"]["state"] == "paused"
    replay = runtime.call("daeri_approval_request", args)
    assert replay["data"]["state"] == "invalidated"
    assert "approval_id" not in replay["data"]
    resumed = runtime.call("daeri_task_resume", {"request_id": "resume", "task_id": saved["task_id"], "expected_task_revision": paused["data"]["task_revision"]})
    assert resumed["status"] == "pending"
    assert resumed["data"]["resumed"] is False
    assert resumed["data"]["state"] == "awaiting_approval"


@pytest.mark.parametrize("decision,expected", [(None, "awaiting_approval"), ("request_changes", "planning"), ("decline", "planning"), ("cancel", "awaiting_approval")])
def test_nonapproval_does_not_grant_execution(configured_runtime, decision, expected):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    waiting = request_approval(runtime, saved)
    result = runtime.call("daeri_approval_request", response_args(saved, waiting, decision=decision, text="조건을 다시 확인해줘"))
    assert "approval_id" not in result["data"]
    task = runtime.call("daeri_task_get", {"task_id": saved["task_id"]})["data"]["task"]
    assert task["state"] == expected


def test_plan_rejects_fabricated_search_and_external_steps(configured_runtime):
    runtime, profile = configured_runtime
    task = create(runtime, profile)
    args = {"request_id": "bad-plan", "task_id": task["task_id"], "expected_task_revision": 1, "draft": draft("invented-record")}
    assert runtime.call("daeri_plan_submit", args)["error"]["code"] == "PLAN_INVALID"
    search = runtime.call("daeri_memory_search", {"task_id": task["task_id"], "query": "검색"})
    args["request_id"] = "external-plan"
    args["draft"] = draft(search["data"]["memory_search_ref"])
    args["draft"]["steps"] = [{"capability_id": "arbitrary_shell"}]
    assert runtime.call("daeri_plan_submit", args)["error"]["code"] == "INVALID_INPUT"


def test_stale_response_cannot_approve_replacement_plan(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    waiting = request_approval(runtime, saved)
    search = runtime.call("daeri_memory_search", {"task_id": saved["task_id"], "query": "수정 계획"})
    revised = draft(search["data"]["memory_search_ref"])
    revised["previous_plan_id"] = saved["plan_id"]
    replacement = runtime.call("daeri_plan_submit", {"request_id": "replace", "task_id": saved["task_id"], "expected_task_revision": waiting["task_revision"], "draft": revised})
    assert replacement["status"] == "ok"
    late = response_args(saved, waiting)
    late["expected_task_revision"] = replacement["data"]["task_revision"]
    assert runtime.call("daeri_approval_request", late)["error"]["code"] == "APPROVAL_UNAVAILABLE"


def test_other_active_owner_cannot_mutate_or_claim_task(configured_runtime):
    runtime, profile = configured_runtime
    task = create(runtime, profile)
    other = Runtime(runtime.store.data_dir)
    try:
        result = other.call("daeri_task_resume", {"request_id": "takeover", "task_id": task["task_id"], "expected_task_revision": 1})
        assert result["error"]["code"] == "OWNER_CONFLICT"
    finally:
        other.close()


def test_expired_plan_body_and_approval_replay_are_blocked(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    waiting = request_approval(runtime, saved)
    args = response_args(saved, waiting)
    approved = runtime.call("daeri_approval_request", args)
    cancelled = runtime.call("daeri_task_cancel", {"request_id": "cancel-task", "task_id": saved["task_id"], "expected_task_revision": approved["data"]["task_revision"]})
    assert cancelled["data"]["state"] == "cancelled"
    runtime.store.set_task_expiry(saved["task_id"], (datetime.now(UTC)-timedelta(seconds=1)).isoformat())
    assert runtime.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})["error"]["code"] == "RECORD_EXPIRED"
    assert runtime.call("daeri_approval_request", args)["error"]["code"] == "RECORD_EXPIRED"


@pytest.mark.parametrize("expired", [False, True])
def test_planless_cancelled_report_obeys_task_retention(configured_runtime, expired):
    runtime, _ = configured_runtime
    profile = runtime.store.put_profile("cancelled-retention", retention_days=1)
    task = create(runtime, profile)
    cancelled = runtime.call("daeri_task_cancel", {
        "request_id": "cancel-before-plan", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"],
    })
    assert cancelled["status"] == "ok", cancelled
    if expired:
        runtime.store.set_task_expiry(task["task_id"],
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    resumed = runtime.call("daeri_task_resume", {
        "request_id": "read-cancelled", "task_id": task["task_id"],
        "expected_task_revision": cancelled["data"]["task_revision"],
    })
    assert resumed["status"] == "ok", resumed
    before = runtime.store.list_records(task["task_id"])
    result = runtime.call("daeri_result_submit", {
        "request_id": "cancelled-report", "task_id": task["task_id"],
        "expected_task_revision": resumed["data"]["task_revision"],
        "plan_id": None, "plan_version": None,
        "report": {"summary": "계획 작성 전 사용자 요청으로 취소했습니다.",
            "proposed_task_state": "cancelled", "findings": [], "tables": [],
            "checks": [], "limitations": [], "uncertain_effects": [],
            "artifacts": [], "next_actions": []},
    })
    if expired:
        assert result["status"] == "error", result
        assert result["error"]["code"] == "RECORD_EXPIRED"
        assert result["error"]["next_action"] == "stop"
        assert runtime.store.list_records(task["task_id"]) == before
    else:
        assert result["status"] == "ok", result
        assert result["data"]["task_state"] == "cancelled"
        assert result["data"]["validation"]["completion"] == "unknown"


def test_credential_patterns_are_redacted_before_persistence(configured_runtime):
    runtime, profile = configured_runtime
    task = create(runtime, profile, text="접속 password=known-sensitive-value")
    request = runtime.store.read_record(task["task_id"], task["request_ref"])
    assert "known-sensitive-value" not in str(request)
    assert "REDACTED" in str(request)


def test_recent_task_pages_follow_activity_and_reject_changed_or_foreign_cursors(configured_runtime):
    runtime, profile = configured_runtime
    first = create(runtime, profile, key="one", text="첫 번째 요청")
    second = create(runtime, profile, key="two", text="두 번째 요청")
    args = {"profile_id": profile["profile_id"], "limit": 1}
    page = runtime.call("daeri_task_get", args)["data"]
    assert page["tasks"][0]["task_id"] == second["task_id"]
    assert page["tasks"][0]["title"] == "두 번째 요청"
    next_page = runtime.call("daeri_task_get", {**args, "cursor": page["next_cursor"]})["data"]
    assert next_page["tasks"][0]["task_id"] == first["task_id"]
    assert next_page["has_more"] is False
    other = runtime.store.put_profile("other")
    foreign = runtime.call("daeri_task_get", {"profile_id": other["profile_id"], "cursor": page["next_cursor"]})
    assert foreign["error"]["code"] == "CURSOR_INVALID"
    runtime.call("daeri_task_update", {"request_id": "question", "task_id": first["task_id"], "expected_task_revision": 1, "patch": {"question": "어느 기간인가요?"}})
    changed = runtime.call("daeri_task_get", {**args, "cursor": page["next_cursor"]})
    assert changed["error"]["code"] == "CURSOR_INVALID"


def test_bodies_never_enter_sqlite_and_expiry_blocks_feedback_replay(configured_runtime):
    runtime, profile = configured_runtime
    task = create(runtime, profile, text="UNIQUE_REQUEST_TEXT")
    question = runtime.call("daeri_task_update", {"request_id": "question", "task_id": task["task_id"], "expected_task_revision": 1, "patch": {"question": "UNIQUE_QUESTION_TEXT"}})["data"]
    answered = runtime.call("daeri_task_update", {"request_id": "answer", "task_id": task["task_id"], "expected_task_revision": question["task_revision"], "patch": {"clarification": "UNIQUE_CLARIFICATION_TEXT"}})["data"]
    saved = plan(runtime, answered)
    waiting = request_approval(runtime, saved)
    runtime.call("daeri_approval_request", response_args(saved, waiting, text="UNIQUE_FEEDBACK_TEXT"))
    for path in runtime.store.data_dir.glob("daeri.sqlite3*"):
        content = path.read_bytes()
        for body in ("UNIQUE_REQUEST_TEXT", "UNIQUE_QUESTION_TEXT", "UNIQUE_CLARIFICATION_TEXT", "UNIQUE_FEEDBACK_TEXT"):
            assert body.encode() not in content


def test_unrelated_profile_description_change_preserves_plan_approval(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    waiting = request_approval(runtime, saved)
    runtime.store.put_profile(profile["name"], core_context=["변경된 정책"])
    result = runtime.call("daeri_approval_request", response_args(saved, waiting))
    assert result["status"] == "ok", result
    assert runtime.store.get_task(saved["task_id"])["state"] == "running"


def test_user_message_reference_cannot_approve_a_different_plan(configured_runtime):
    runtime, profile = configured_runtime
    first = plan(runtime, create(runtime, profile))
    waiting = request_approval(runtime, first)
    args = response_args(first, waiting, decision="request_changes", text="다시 검토해줘")
    args["user_message_ref"] = "actual-message-1"
    assert runtime.call("daeri_approval_request", args)["status"] == "ok"
    second = plan(runtime, create(runtime, profile, key="other-task"), key="other-plan")
    other_waiting = request_approval(runtime, second)
    reused = response_args(second, other_waiting)
    reused["user_message_ref"] = "actual-message-1"
    assert runtime.call("daeri_approval_request", reused)["error"]["code"] == "APPROVAL_UNAVAILABLE"


def test_rejected_plan_can_be_corrected_with_the_same_optional_legacy_key(configured_runtime):
    runtime, profile = configured_runtime
    task = create(runtime, profile)
    args = {"request_id": "plan", "task_id": task["task_id"], "draft": draft("fake")}
    assert runtime.call("daeri_plan_submit", args)["error"]["code"] == "PLAN_INVALID"
    searched = runtime.call("daeri_memory_search", {"task_id": task["task_id"], "query": "업무"})
    args["draft"] = draft(searched["data"]["memory_search_ref"])
    assert runtime.call("daeri_plan_submit", args)["status"] == "ok"


def test_plan_get_uses_saved_plan_wrapper_and_reports_search_limitations(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    fetched = runtime.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})
    assert set(fetched["data"]) == {"task_id", "task_revision", "plan"}
    assert fetched["data"]["plan"]["plan_id"] == saved["plan_id"]
    assert fetched["data"]["plan"]["draft"]["memory_limitations"]


def test_paused_question_resume_never_caches_its_body_in_sqlite(configured_runtime):
    runtime, profile = configured_runtime
    task = create(runtime, profile)
    question = runtime.call("daeri_task_update", {"request_id": "question", "task_id": task["task_id"], "expected_task_revision": 1, "patch": {"question": "PRIVATE_PAUSED_QUESTION"}})["data"]
    paused = runtime.call("daeri_task_update", {"request_id": "pause", "task_id": task["task_id"], "expected_task_revision": question["task_revision"], "action": "pause"})["data"]
    args = {"request_id": "resume", "task_id": task["task_id"], "expected_task_revision": paused["task_revision"]}
    resumed = runtime.call("daeri_task_resume", args)
    assert resumed["data"]["waiting_reason"]["message"] == "PRIVATE_PAUSED_QUESTION"
    assert runtime.call("daeri_task_resume", args) == resumed
    for path in runtime.store.data_dir.glob("daeri.sqlite3*"):
        assert b"PRIVATE_PAUSED_QUESTION" not in path.read_bytes()


def test_unrelated_profile_change_keeps_approval_when_replaying_old_response(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    waiting = request_approval(runtime, saved)
    args = response_args(saved, waiting)
    runtime.call("daeri_approval_request", args)
    runtime.store.put_profile(profile["name"], core_context=["새 운영 정책"])
    replay = runtime.call("daeri_approval_request", args)
    assert replay["data"]["state"] == "approved"
    assert replay["data"]["approval_id"]
    current = runtime.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})
    assert current["data"]["plan"]["approval_request"]["state"] == "approved"


def test_unpaused_resume_preserves_valid_approval(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    approved = runtime.call("daeri_approval_request", response_args(saved, request_approval(runtime, saved)))["data"]
    resumed = runtime.call("daeri_task_resume", {"request_id": "resume", "task_id": saved["task_id"], "expected_task_revision": approved["task_revision"]})
    assert resumed["data"]["state"] == "running"
    assert resumed["data"]["resumed"] is True
    current = runtime.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})
    assert current["data"]["plan"]["approval_request"]["approval_id"] == approved["approval_id"]


def test_same_local_user_can_respond_from_another_connection_without_taking_ownership(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    waiting = request_approval(runtime, saved)
    other = Runtime(runtime.store.data_dir)
    try:
        result = other.call("daeri_approval_request", response_args(saved, waiting))
        assert result["status"] == "ok"
        assert result["data"]["approval_id"]
        fetched = other.call("daeri_task_get", {"task_id": saved["task_id"]})["data"]["task"]
        assert fetched["execution_owner_status"] == "other"
    finally:
        other.close()


def test_task_exposes_a_workspace_for_host_generated_files(configured_runtime):
    runtime, profile = configured_runtime
    task = create(runtime, profile)
    from pathlib import Path
    workspace = Path(task["artifact_workspace"])
    assert workspace.is_absolute() and workspace.is_dir()
    fetched = runtime.call("daeri_task_get", {"task_id": task["task_id"]})
    assert fetched["data"]["task"]["artifact_workspace"] == str(workspace)


def test_new_plan_does_not_store_deprecated_empty_generator_rules(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    fetched = runtime.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})
    assert set(fetched["data"]["plan"]["draft"]["output"]) == {"format", "destination", "resource_rules"}


def test_new_plan_rejects_legacy_table_generation_rules(configured_runtime):
    runtime, profile = configured_runtime
    task = create(runtime, profile)
    search = runtime.call("daeri_memory_search", {"task_id": task["task_id"], "query": "출력"})
    value = draft(search["data"]["memory_search_ref"])
    value["output"]["artifact_rules"] = [{"artifact_id": "report", "format_id": "text", "format_contract_ref": "format_text_v1", "input_kind": "report", "options": {}, "max_bytes": 1000}]
    result = runtime.call("daeri_plan_submit", {"request_id": "legacy-output", "task_id": task["task_id"], "expected_task_revision": task["task_revision"], "draft": value})
    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_INPUT"


def test_stored_legacy_plan_remains_readable(configured_runtime):
    runtime, profile = configured_runtime
    saved = plan(runtime, create(runtime, profile))
    task = runtime.store.get_task(saved["task_id"])
    header = task["plans"][0]
    legacy = runtime.store.read_record(task["task_id"], header["body_ref"])["payload"]["draft"]
    legacy["output"]["table_rules"] = [{"table_id": "old-table", "sources": [], "join_mode": "none"}]
    legacy["output"]["artifact_rules"] = []
    with runtime.store.transaction():
        header["body_ref"] = runtime.store.write_record(task["task_id"], "plan", {"draft": legacy})
        runtime._save(task)
    fetched = runtime.call("daeri_plan_get", {"task_id": task["task_id"], "plan_id": saved["plan_id"]})
    assert fetched["data"]["plan"]["draft"]["output"]["table_rules"] == legacy["output"]["table_rules"]


def _finished_read_report(runtime, profile):
    saved, value = begin(runtime, profile)
    approved = approve(runtime, saved)
    references = []
    for index, step in enumerate(value["steps"]):
        approved["task_revision"] = runtime.store.get_task(saved["task_id"])["task_revision"]
        observed = runtime.call("daeri_capability_execute", execution(saved, approved, step, call_id=f"read-{index}"))
        assert observed["status"] == "ok", observed
        references.append(observed["data"]["result_ref"])
    report = {"summary": "승인된 조회 결과를 확인했다", "proposed_task_state": "succeeded", "findings": [], "tables": [],
        "checks": [{"criterion_id": "read", "status": "met", "method": "호스트가 요청과 관찰을 대조했다", "evidence_refs": references}],
        "limitations": [], "uncertain_effects": [], "artifacts": [], "next_actions": []}
    stored = runtime.call("daeri_result_submit", {"request_id": "report", "task_id": saved["task_id"],
        "expected_task_revision": runtime.store.get_task(saved["task_id"])["task_revision"],
        "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "report": report})
    assert stored["status"] == "ok", stored
    return saved["task_id"], stored["data"]["report_ref"], references[0]


def _save_profile(runtime, profile):
    return runtime.store.put_profile(profile["name"], **{key: profile[key] for key in
        ("core_context", "resources", "retention_days", "long_term_memory_days", "memory_policy", "embedding")})


def test_archived_call_survives_new_capabilities_on_the_same_resource(configured):
    import copy
    runtime, profile = configured
    saved, value = begin(runtime, profile)
    approved = approve(runtime, saved)
    observed = runtime.call("daeri_capability_execute", execution(saved, approved, value["steps"][0]))["data"]
    runtime.call("daeri_task_cancel", {"request_id": "cancel", "task_id": saved["task_id"], "expected_task_revision": observed["task_revision"]})
    updated = copy.deepcopy(profile)
    extra = copy.deepcopy(updated["resources"][0]["capabilities"][0])
    extra["capability_id"] = "another-read"
    updated["resources"][0]["capabilities"].append(extra)
    _save_profile(runtime, updated)
    result = runtime.call("daeri_result_get", {"task_id": saved["task_id"], "record_ref": observed["result_ref"]})
    assert result["status"] == "ok", result
    assert result["data"]["payload"]["data"]["rows"][0]["amount"] == "10.00"


@pytest.mark.parametrize("change", ["same", "context", "unrelated_resource"])
def test_archived_report_remains_readable_after_unrelated_profile_reconfiguration(configured, change):
    import copy
    runtime, profile = configured
    task_id, report_ref, call_ref = _finished_read_report(runtime, profile)
    updated = copy.deepcopy(profile)
    if change == "context":
        updated["core_context"] = ["새 기본 설명"]
    elif change == "unrelated_resource":
        extra = copy.deepcopy(updated["resources"][0])
        extra["instance_id"] = "cache-c"
        updated["resources"].append(extra)
    _save_profile(runtime, updated)
    for ref in (report_ref, call_ref):
        result = runtime.call("daeri_result_get", {"task_id": task_id, "record_ref": ref})
        assert result["status"] == "ok", result


@pytest.mark.parametrize("change", ["target", "model_policy", "removed"])
def test_archived_report_rechecks_its_actual_sources_under_current_permissions(configured, change):
    import copy
    runtime, profile = configured
    task_id, report_ref, _ = _finished_read_report(runtime, profile)
    updated = copy.deepcopy(profile)
    if change == "target":
        updated["resources"][0]["target"]["host"] = "other.invalid"
        updated["resources"][0]["connection"]["env"]["REDIS_HOST"] = "other.invalid"
    elif change == "model_policy":
        updated["resources"][0]["policy"]["allowed_model_fields"] = []
    else:
        updated["resources"].pop(0)
        if updated["memory_policy"]:
            updated["memory_policy"]["instance_ids"] = ["cache-b"]
    _save_profile(runtime, updated)
    result = runtime.call("daeri_result_get", {"task_id": task_id, "record_ref": report_ref})
    assert result["status"] == "error"
    assert result["error"]["code"] == "ACCESS_DENIED", result


@pytest.fixture
def bundle_runtime(tmp_path):
    import json
    from daeri.infrastructure.resources import ResourceRegistry
    from test_resource_autonomy import lower_fixture
    item = resource()
    item["discover_tools"] = True
    item["capabilities"][0]["constraints"] = {}
    item["connection"]["args"] = ["-I", str(lower_fixture(tmp_path))]
    item["connection"]["env"]["FIXTURE_TOOLS"] = json.dumps([["get", True], ["scan_keys", True], ["set", False]])
    store = Store(tmp_path / "data")
    profile = store.put_profile("service", resources=[item])
    ResourceRegistry(store).diagnose(profile, "cache-a")
    store.close()
    runtime = Runtime(tmp_path / "data")
    yield runtime, profile
    runtime.close()


def bundle_plan(runtime, profile, *, scope=None):
    bundle = runtime.registry.get(profile, "cache-a", "*")
    def configure(value):
        value["steps"][0].update(capability_id="*", contract_ref=bundle["contract_ref"], scope={} if scope is None else scope)
    return begin(runtime, profile, max_calls=4, configure=configure)


def test_approved_resource_bundle_allows_host_tool_choice_and_preserves_both_identities(bundle_runtime):
    runtime, profile = bundle_runtime
    saved, value = bundle_plan(runtime, profile)
    approved = approve(runtime, saved)
    references = []
    for index, (capability, parameters) in enumerate([("get", {"key": "orders"}), ("scan_keys", {"pattern": "orders:*"})]):
        args = execution(saved, approved, value["steps"][0], call_id=f"choose-{index}")
        args.update(capability_id=capability, parameters=parameters,
            expected_task_revision=runtime.store.get_task(saved["task_id"])["task_revision"])
        result = runtime.call("daeri_capability_execute", args)
        assert result["status"] == "ok", result
        assert result["data"]["inline_result"]["data"]["tool"] == capability
        assert result["data"]["actual_capability_id"] == capability
        call = runtime.store.get_task(saved["task_id"])["calls"][-1]
        assert call["capability_id"] == "*"
        assert call["contract_ref"] == saved["contract_refs"][0]
        assert call["actual_capability_id"] == capability
        assert call["actual_contract_ref"] == runtime.registry.get(profile, "cache-a", capability)["contract_ref"]
        body = runtime.store.read_record(saved["task_id"], result["data"]["result_ref"])["payload"]
        assert body["capability_id"] == "*" and body["actual_capability_id"] == capability
        assert runtime.call("daeri_capability_execute", args)["data"]["result_ref"] == result["data"]["result_ref"]
        references.append(result["data"]["result_ref"])
    report = {"summary": "호스트가 필요한 두 조회 도구를 골라 확인했다", "proposed_task_state": "succeeded", "findings": [],
        "checks": [{"criterion_id": "read", "status": "met", "method": "현재 관찰 대조", "evidence_refs": references}],
        "limitations": [], "uncertain_effects": [], "artifacts": [], "next_actions": []}
    final = runtime.call("daeri_result_submit", {"request_id": "report", "task_id": saved["task_id"],
        "expected_task_revision": runtime.store.get_task(saved["task_id"])["task_revision"],
        "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "report": report})
    assert final["status"] == "ok", final
    _save_profile(runtime, profile)
    reread = runtime.call("daeri_result_get", {"task_id": saved["task_id"], "record_ref": final["data"]["report_ref"]})
    assert reread["status"] == "ok", reread


@pytest.mark.parametrize("capability", ["set", "*", "invented"])
def test_resource_bundle_cannot_select_a_mutating_or_unknown_tool(bundle_runtime, capability):
    runtime, profile = bundle_runtime
    saved, value = bundle_plan(runtime, profile)
    approved = approve(runtime, saved)
    args = execution(saved, approved, value["steps"][0])
    args["capability_id"] = capability
    result = runtime.call("daeri_capability_execute", args)
    assert result["status"] == "error", result
    assert runtime.store.get_task(saved["task_id"])["calls"] == []


def test_resource_bundle_applies_explicit_scope_to_each_selected_tool(bundle_runtime):
    runtime, profile = bundle_runtime
    saved, value = bundle_plan(runtime, profile, scope={"equals": {"key": "orders"}})
    approved = approve(runtime, saved)
    args = execution(saved, approved, value["steps"][0], key="other")
    args["capability_id"] = "get"
    denied = runtime.call("daeri_capability_execute", args)
    assert denied["error"]["code"] == "SCOPE_VIOLATION", denied
    args["parameters"] = {"key": "orders"}
    assert runtime.call("daeri_capability_execute", args)["status"] == "ok"


def test_resource_bundle_change_requires_a_new_plan_before_dispatch(bundle_runtime):
    import json
    runtime, profile = bundle_runtime
    saved, value = bundle_plan(runtime, profile)
    approved = approve(runtime, saved)
    registration_path = runtime.registry._path(profile, "cache-a")
    registration = json.loads(registration_path.read_text())
    registration["descriptors"] = [item for item in registration["descriptors"] if item["capability_id"] != "scan_keys"]
    registration_path.write_text(json.dumps(registration))
    args = execution(saved, approved, value["steps"][0])
    args["capability_id"] = "get"
    result = runtime.call("daeri_capability_execute", args)
    assert result["error"]["code"] == "CONTRACT_CHANGED", result
    assert runtime.store.get_task(saved["task_id"])["calls"] == []
