from datetime import UTC, datetime, timedelta

import pytest

from daeri.engine import Engine
from daeri.storage import Store


@pytest.mark.parametrize("code,action", [
    ("CALL_ID_CONFLICT", "stop"), ("CONTEXT_CHANGED", "stop"),
    ("AUTHENTICATION_REQUIRED", "stop"), ("RECORD_EXPIRED", "stop"),
    ("CONTRACT_CHANGED", "refresh"), ("MEMORY_SEARCH_INCOMPLETE", "refresh"),
    ("APPROVAL_REQUIRED", "request_approval"), ("BUDGET_EXCEEDED", "request_approval"),
    ("EXTERNAL_OUTCOME_UNKNOWN", "reconcile"), ("INTENT_PERSIST_FAILED", "retry_same_request"),
])
def test_errors_direct_the_host_to_the_contracted_recovery_action(code, action):
    from daeri.engine import response
    assert response(None, "error", code)["error"]["next_action"] == action


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path)
    profile = store.put_profile("service-prod", core_context=["UTC 기준 운영"])
    store.close()
    engine = Engine(tmp_path)
    yield engine, profile
    engine.close()


def create(engine, profile, key="create-1", text="운영 절차를 설명해줘"):
    result = engine.call("daeri_task_create", {
        "request_id": key, "request_text": text,
        "context": {"profile_id": profile["profile_id"]},
    })
    assert result["status"] == "ok", result
    return result["data"]


def draft(search_ref):
    return {
        "purpose": "현재 요청과 설정만 검토", "steps": [],
        "budget": {"max_tool_calls": 0, "max_external_requests": 0, "max_runtime_ms": 0, "resource_limits": []},
        "output": {"format": "text", "destination": "현재 대화", "resource_rules": [], "table_rules": [], "artifact_rules": []},
        "memory_search_refs": [search_ref], "memory_refs": [], "memory_limitations": [],
        "completion_criteria": [{"criterion_id": "explanation", "description": "요청 맥락 설명", "required": True, "step_ids": []}],
    }


def plan(engine, task, key="plan-1"):
    search = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "운영 절차"})
    assert search["status"] == "partial"
    assert search["error"]["code"] == "MEMORY_SEARCH_INCOMPLETE"
    assert search["data"]["retrieval_state"] == "keyword_only"
    assert search["data"]["task_revision"] == task["task_revision"]
    args = {"request_id": key, "task_id": task["task_id"], "expected_task_revision": task["task_revision"], "draft": draft(search["data"]["memory_search_ref"])}
    result = engine.call("daeri_plan_submit", args)
    assert result["status"] == "ok", result
    return result["data"]


def request_approval(engine, saved, key="approval-1"):
    result = engine.call("daeri_approval_request", {"request_id": key, "task_id": saved["task_id"], "expected_task_revision": saved["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "stage": "request"})
    assert result["status"] == "pending", result
    return result["data"]


def response_args(saved, waiting, key="response-1", decision="approve", text="이 계획대로 승인할게"):
    return {"request_id": key, "task_id": saved["task_id"], "expected_task_revision": waiting["task_revision"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "stage": "respond", "approval_request_id": waiting["approval_request_id"], "decision": decision, "user_response": text}


def test_task_survives_restart_without_request_body_in_sqlite(runtime):
    engine, profile = runtime
    body = "UNIQUE_PRIVATE_REQUEST_BODY"
    task = create(engine, profile, text=body)
    assert task["state"] == "planning"
    other = Engine(engine.store.data_dir)
    try:
        fetched = other.call("daeri_task_get", {"task_id": task["task_id"]})
        assert fetched["data"]["task"]["request_ref"] == task["request_ref"]
        assert fetched["data"]["task"]["execution_owner_status"] == "other"
    finally:
        other.close()
    for path in engine.store.data_dir.glob("daeri.sqlite3*"):
        assert body.encode() not in path.read_bytes()


def test_create_replay_is_idempotent_and_different_input_conflicts(runtime):
    engine, profile = runtime
    first = create(engine, profile)
    again = create(engine, profile)
    assert first == again
    conflict = engine.call("daeri_task_create", {"request_id": "create-1", "request_text": "다른 업무", "context": {"profile_id": profile["profile_id"]}})
    assert conflict["error"]["code"] == "REQUEST_ID_CONFLICT"
    assert len(engine.store.list_tasks(profile["profile_id"])) == 1


def test_missing_profile_waits_for_explicit_selection(runtime):
    engine, profile = runtime
    task = engine.call("daeri_task_create", {"request_id": "unresolved", "request_text": "상태 확인"})["data"]
    assert task["state"] == "awaiting_input"
    assert task["context"]["profile_id"] is None
    bound = engine.call("daeri_task_update", {"request_id": "bind", "task_id": task["task_id"], "expected_task_revision": 1, "patch": {"context": {"profile_id": profile["profile_id"]}}})
    assert bound["status"] == "ok"
    assert bound["data"]["state"] == "planning"


def test_approval_requires_actual_response_and_replays_current_validity(runtime):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    waiting = request_approval(engine, saved)
    missing = response_args(saved, waiting)
    missing.pop("user_response")
    assert engine.call("daeri_approval_request", missing)["error"]["code"] == "INVALID_INPUT"
    args = response_args(saved, waiting)
    approved = engine.call("daeri_approval_request", args)
    assert approved["status"] == "ok"
    assert approved["data"]["approval_id"]
    revision = approved["data"]["task_revision"]
    assert engine.call("daeri_approval_request", args)["data"]["task_revision"] == revision
    paused = engine.call("daeri_task_update", {"request_id": "pause", "task_id": saved["task_id"], "expected_task_revision": revision, "action": "pause"})
    assert paused["data"]["state"] == "paused"
    replay = engine.call("daeri_approval_request", args)
    assert replay["data"]["state"] == "invalidated"
    assert "approval_id" not in replay["data"]
    resumed = engine.call("daeri_task_resume", {"request_id": "resume", "task_id": saved["task_id"], "expected_task_revision": paused["data"]["task_revision"]})
    assert resumed["status"] == "pending"
    assert resumed["data"]["resumed"] is False
    assert resumed["data"]["state"] == "awaiting_approval"


@pytest.mark.parametrize("decision,expected", [(None, "awaiting_approval"), ("request_changes", "planning"), ("decline", "planning"), ("cancel", "awaiting_approval")])
def test_nonapproval_does_not_grant_execution(runtime, decision, expected):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    waiting = request_approval(engine, saved)
    result = engine.call("daeri_approval_request", response_args(saved, waiting, decision=decision, text="조건을 다시 확인해줘"))
    assert "approval_id" not in result["data"]
    task = engine.call("daeri_task_get", {"task_id": saved["task_id"]})["data"]["task"]
    assert task["state"] == expected


def test_plan_rejects_fabricated_search_and_external_steps(runtime):
    engine, profile = runtime
    task = create(engine, profile)
    args = {"request_id": "bad-plan", "task_id": task["task_id"], "expected_task_revision": 1, "draft": draft("invented-record")}
    assert engine.call("daeri_plan_submit", args)["error"]["code"] == "PLAN_INVALID"
    search = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "검색"})
    args["request_id"] = "external-plan"
    args["draft"] = draft(search["data"]["memory_search_ref"])
    args["draft"]["steps"] = [{"capability_id": "arbitrary_shell"}]
    assert engine.call("daeri_plan_submit", args)["error"]["code"] == "INVALID_INPUT"


def test_stale_response_cannot_approve_replacement_plan(runtime):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    waiting = request_approval(engine, saved)
    search = engine.call("daeri_memory_search", {"task_id": saved["task_id"], "query": "수정 계획"})
    revised = draft(search["data"]["memory_search_ref"])
    revised["previous_plan_id"] = saved["plan_id"]
    replacement = engine.call("daeri_plan_submit", {"request_id": "replace", "task_id": saved["task_id"], "expected_task_revision": waiting["task_revision"], "draft": revised})
    assert replacement["status"] == "ok"
    late = response_args(saved, waiting)
    late["expected_task_revision"] = replacement["data"]["task_revision"]
    assert engine.call("daeri_approval_request", late)["error"]["code"] == "APPROVAL_UNAVAILABLE"


def test_other_active_owner_cannot_mutate_or_claim_task(runtime):
    engine, profile = runtime
    task = create(engine, profile)
    other = Engine(engine.store.data_dir)
    try:
        result = other.call("daeri_task_resume", {"request_id": "takeover", "task_id": task["task_id"], "expected_task_revision": 1})
        assert result["error"]["code"] == "OWNER_CONFLICT"
    finally:
        other.close()


def test_expired_plan_body_and_approval_replay_are_blocked(runtime):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    waiting = request_approval(engine, saved)
    args = response_args(saved, waiting)
    approved = engine.call("daeri_approval_request", args)
    cancelled = engine.call("daeri_task_cancel", {"request_id": "cancel-task", "task_id": saved["task_id"], "expected_task_revision": approved["data"]["task_revision"]})
    assert cancelled["data"]["state"] == "cancelled"
    engine.store.set_task_expiry(saved["task_id"], (datetime.now(UTC)-timedelta(seconds=1)).isoformat())
    assert engine.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})["error"]["code"] == "RECORD_EXPIRED"
    assert engine.call("daeri_approval_request", args)["error"]["code"] == "RECORD_EXPIRED"


def test_credential_patterns_are_redacted_before_persistence(runtime):
    engine, profile = runtime
    task = create(engine, profile, text="접속 password=known-sensitive-value")
    request = engine.store.read_record(task["task_id"], task["request_ref"])
    assert "known-sensitive-value" not in str(request)
    assert "REDACTED" in str(request)


def test_recent_task_pages_follow_activity_and_reject_changed_or_foreign_cursors(runtime):
    engine, profile = runtime
    first = create(engine, profile, key="one", text="첫 번째 요청")
    second = create(engine, profile, key="two", text="두 번째 요청")
    args = {"profile_id": profile["profile_id"], "limit": 1}
    page = engine.call("daeri_task_get", args)["data"]
    assert page["tasks"][0]["task_id"] == second["task_id"]
    assert page["tasks"][0]["title"] == "두 번째 요청"
    next_page = engine.call("daeri_task_get", {**args, "cursor": page["next_cursor"]})["data"]
    assert next_page["tasks"][0]["task_id"] == first["task_id"]
    assert next_page["has_more"] is False
    other = engine.store.put_profile("other")
    foreign = engine.call("daeri_task_get", {"profile_id": other["profile_id"], "cursor": page["next_cursor"]})
    assert foreign["error"]["code"] == "CURSOR_INVALID"
    engine.call("daeri_task_update", {"request_id": "question", "task_id": first["task_id"], "expected_task_revision": 1, "patch": {"question": "어느 기간인가요?"}})
    changed = engine.call("daeri_task_get", {**args, "cursor": page["next_cursor"]})
    assert changed["error"]["code"] == "CURSOR_INVALID"


def test_bodies_never_enter_sqlite_and_expiry_blocks_feedback_replay(runtime):
    engine, profile = runtime
    task = create(engine, profile, text="UNIQUE_REQUEST_TEXT")
    question = engine.call("daeri_task_update", {"request_id": "question", "task_id": task["task_id"], "expected_task_revision": 1, "patch": {"question": "UNIQUE_QUESTION_TEXT"}})["data"]
    answered = engine.call("daeri_task_update", {"request_id": "answer", "task_id": task["task_id"], "expected_task_revision": question["task_revision"], "patch": {"clarification": "UNIQUE_CLARIFICATION_TEXT"}})["data"]
    saved = plan(engine, answered)
    waiting = request_approval(engine, saved)
    engine.call("daeri_approval_request", response_args(saved, waiting, text="UNIQUE_FEEDBACK_TEXT"))
    for path in engine.store.data_dir.glob("daeri.sqlite3*"):
        content = path.read_bytes()
        for body in ("UNIQUE_REQUEST_TEXT", "UNIQUE_QUESTION_TEXT", "UNIQUE_CLARIFICATION_TEXT", "UNIQUE_FEEDBACK_TEXT"):
            assert body.encode() not in content


def test_profile_change_between_display_and_approval_does_not_grant_authority(runtime):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    waiting = request_approval(engine, saved)
    engine.store.put_profile(profile["name"], core_context=["변경된 정책"])
    result = engine.call("daeri_approval_request", response_args(saved, waiting))
    assert result["error"]["code"] == "PLAN_INVALID"
    assert engine.store.get_task(saved["task_id"])["state"] == "awaiting_approval"


def test_user_message_reference_cannot_approve_a_different_plan(runtime):
    engine, profile = runtime
    first = plan(engine, create(engine, profile))
    waiting = request_approval(engine, first)
    args = response_args(first, waiting, decision="request_changes", text="다시 검토해줘")
    args["user_message_ref"] = "actual-message-1"
    assert engine.call("daeri_approval_request", args)["status"] == "ok"
    second = plan(engine, create(engine, profile, key="other-task"), key="other-plan")
    other_waiting = request_approval(engine, second)
    reused = response_args(second, other_waiting)
    reused["user_message_ref"] = "actual-message-1"
    assert engine.call("daeri_approval_request", reused)["error"]["code"] == "APPROVAL_UNAVAILABLE"


def test_failed_plan_replay_keeps_one_check_and_rejects_changed_input(runtime):
    engine, profile = runtime
    task = create(engine, profile)
    bad = {"request_id": "plan", "task_id": task["task_id"], "expected_task_revision": 1, "draft": draft("fake")}
    failed = engine.call("daeri_plan_submit", bad)
    assert failed["error"]["code"] == "PLAN_INVALID"
    assert engine.call("daeri_plan_submit", bad) == failed
    checks = [item for item in engine.store.list_records(task["task_id"]) if item["record_kind"] == "local_check"]
    assert len(checks) == 1
    check = engine.store.read_record(task["task_id"], checks[0]["record_ref"])["payload"]
    assert check["outcome"] == "failed"
    assert check["error_code"] == "PLAN_INVALID"
    bad["draft"]["purpose"] = "변경한 목적"
    assert engine.call("daeri_plan_submit", bad)["error"]["code"] == "REQUEST_ID_CONFLICT"


def test_plan_get_uses_saved_plan_wrapper_and_reports_search_limitations(runtime):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    fetched = engine.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})
    assert set(fetched["data"]) == {"task_id", "task_revision", "plan"}
    assert fetched["data"]["plan"]["plan_id"] == saved["plan_id"]
    assert fetched["data"]["plan"]["draft"]["memory_limitations"]


def test_paused_question_resume_never_caches_its_body_in_sqlite(runtime):
    engine, profile = runtime
    task = create(engine, profile)
    question = engine.call("daeri_task_update", {"request_id": "question", "task_id": task["task_id"], "expected_task_revision": 1, "patch": {"question": "PRIVATE_PAUSED_QUESTION"}})["data"]
    paused = engine.call("daeri_task_update", {"request_id": "pause", "task_id": task["task_id"], "expected_task_revision": question["task_revision"], "action": "pause"})["data"]
    args = {"request_id": "resume", "task_id": task["task_id"], "expected_task_revision": paused["task_revision"]}
    resumed = engine.call("daeri_task_resume", args)
    assert resumed["data"]["waiting_reason"]["message"] == "PRIVATE_PAUSED_QUESTION"
    assert engine.call("daeri_task_resume", args) == resumed
    for path in engine.store.data_dir.glob("daeri.sqlite3*"):
        assert b"PRIVATE_PAUSED_QUESTION" not in path.read_bytes()


def test_profile_change_invalidates_approval_even_when_replaying_old_response(runtime):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    waiting = request_approval(engine, saved)
    args = response_args(saved, waiting)
    engine.call("daeri_approval_request", args)
    engine.store.put_profile(profile["name"], core_context=["새 운영 정책"])
    replay = engine.call("daeri_approval_request", args)
    assert replay["data"]["state"] == "invalidated"
    assert "approval_id" not in replay["data"]
    current = engine.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})
    assert current["data"]["plan"]["approval_request"]["state"] == "invalidated"


def test_unpaused_resume_preserves_valid_approval(runtime):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    approved = engine.call("daeri_approval_request", response_args(saved, request_approval(engine, saved)))["data"]
    resumed = engine.call("daeri_task_resume", {"request_id": "resume", "task_id": saved["task_id"], "expected_task_revision": approved["task_revision"]})
    assert resumed["data"]["state"] == "running"
    assert resumed["data"]["resumed"] is True
    current = engine.call("daeri_plan_get", {"task_id": saved["task_id"], "plan_id": saved["plan_id"]})
    assert current["data"]["plan"]["approval_request"]["approval_id"] == approved["approval_id"]


def test_same_local_user_can_respond_from_another_connection_without_taking_ownership(runtime):
    engine, profile = runtime
    saved = plan(engine, create(engine, profile))
    waiting = request_approval(engine, saved)
    other = Engine(engine.store.data_dir)
    try:
        result = other.call("daeri_approval_request", response_args(saved, waiting))
        assert result["status"] == "ok"
        assert result["data"]["approval_id"]
        fetched = other.call("daeri_task_get", {"task_id": saved["task_id"]})["data"]["task"]
        assert fetched["execution_owner_status"] == "other"
    finally:
        other.close()
