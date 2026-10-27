"""Copied memory in a displayed plan follows the original knowledge's access rules."""

import copy
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from daeri.application.runtime import Runtime
from test_automatic_memory import completed
from test_memory_flow import new_task, remembered


@pytest.mark.parametrize("change,code,restart_code", [
    ("correct", "MEMORY_CONFLICT", "MEMORY_CONFLICT"),
    ("invalidate", "MEMORY_CONFLICT", "MEMORY_CONFLICT"),
    ("expire", "RECORD_EXPIRED", "RECORD_EXPIRED"),
    ("permission", "ACCESS_DENIED", "RECORD_EXPIRED"),
    ("target", "ACCESS_DENIED", "RECORD_EXPIRED"),
])
def test_plan_display_blocks_changed_memory_but_keeps_task_recovery(tmp_path, change, code, restart_code):
    with closing(Runtime(tmp_path)) as runtime:
        profile, report_args = completed(runtime)
        reported = runtime.call("daeri_result_submit", report_args)["data"]
        memory_ref = reported["memory"]["items"][0]["memory_ref"]
        item = runtime.memory.get(profile["profile_id"], memory_ref)
        task = runtime.call("daeri_task_create", {"request_id": "next", "request_text": "Cache get",
            "context": {"profile_id": profile["profile_id"]}})["data"]
        searched = runtime.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache get"})["data"]
        draft = copy.deepcopy(runtime.call("daeri_plan_get", {"task_id": report_args["task_id"],
            "plan_id": report_args["plan_id"]})["data"]["plan"]["draft"])
        draft.update(purpose=item["statement"], memory_search_refs=[searched["memory_search_ref"]],
            memory_refs=[memory_ref])
        plan_args = {"request_id": "next-plan", "task_id": task["task_id"],
            "expected_task_revision": task["task_revision"], "draft": draft}
        saved = runtime.call("daeri_plan_submit", plan_args)["data"]
        lookup = {"task_id": task["task_id"], "plan_id": saved["plan_id"]}
        assert runtime.call("daeri_plan_get", lookup)["data"]["plan"]["draft"]["purpose"] == item["statement"]

        if change in {"correct", "invalidate"}:
            update = {"request_id": "change-memory", "task_id": report_args["task_id"],
                "expected_task_revision": reported["task_revision"], "memory_ref": memory_ref,
                "operation": "invalidate", "source_refs": report_args["evidence_refs"],
                "reason": "실제 근거를 대조해 이전 지식을 정정함"}
            if change == "correct":
                update.update(operation="store", draft={key: item[key] for key in (
                    "category", "statement", "applicability", "instance_ids", "verification_method")})
                update["draft"]["statement"] = "새 확인 결과로 절차의 적용 조건을 정정했다."
            changed = runtime.call("daeri_memory_update", update)
            assert changed["status"] == "ok", changed
        elif change == "expire":
            with runtime.store.transaction():
                runtime.store._db.execute("UPDATE memory_versions SET expires_at=? WHERE memory_ref=?",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), memory_ref))
        else:
            resources = copy.deepcopy(profile["resources"])
            if change == "target":
                resources[0]["target"]["db"] = 1
            runtime.store.put_profile(profile["name"], resources=resources,
                embedding={"provider": "keyword_only"},
                memory_policy={"model_allowed": False} if change == "permission" else None)

        blocked = runtime.call("daeri_plan_get", lookup)
        assert blocked["status"] == "error" and blocked["error"]["code"] == code, blocked
        assert blocked["data"] is None
        # Startup cleanup removes denied memory bodies; metadata remains available.
        with closing(Runtime(tmp_path)) as reader:
            blocked = reader.call("daeri_plan_get", lookup)
            assert blocked["status"] == "error", blocked
            assert blocked["error"]["code"] == restart_code
            assert blocked["data"] is None
            snapshot = reader.call("daeri_task_get", {"task_id": task["task_id"]})
            assert snapshot["status"] == "ok", snapshot
            assert snapshot["data"]["task"]["active_plan_ref"]["plan_id"] == saved["plan_id"]
            assert item["statement"] not in json.dumps(snapshot, ensure_ascii=False)
            recent = reader.call("daeri_task_get", {"profile_id": profile["profile_id"]})
            assert recent["status"] == "ok" and any(row["task_id"] == task["task_id"] for row in recent["data"]["tasks"])
        assert runtime.call("daeri_plan_submit", plan_args)["status"] == "ok"

        if change in {"correct", "invalidate", "expire"}:
            resumed = runtime.call("daeri_task_resume", {"request_id": "resume", "task_id": task["task_id"],
                "expected_task_revision": saved["task_revision"]})
            assert resumed["status"] == "pending" and resumed["data"]["state"] == "awaiting_approval", resumed
            searched = runtime.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache get"})["data"]
            draft.update(purpose="현재 관찰로 적용 조건을 다시 확인한다", previous_plan_id=saved["plan_id"],
                memory_search_refs=[searched["memory_search_ref"]], memory_refs=[])
            revised = runtime.call("daeri_plan_submit", {"request_id": "replan", "task_id": task["task_id"],
                "expected_task_revision": resumed["data"]["task_revision"], "draft": draft})
            assert revised["status"] == "ok", revised
            assert runtime.call("daeri_plan_get", {"task_id": task["task_id"],
                "plan_id": revised["data"]["plan_id"]})["status"] == "ok"
            assert runtime.call("daeri_plan_get", lookup)["status"] == "error"
            assert runtime.call("daeri_task_get", {"task_id": task["task_id"]})["data"]["task"]["state"] == "awaiting_approval"
        snapshot = runtime.call("daeri_task_get", {"task_id": task["task_id"]})["data"]
        cancelled = runtime.call("daeri_task_cancel", {"request_id": "cancel", "task_id": task["task_id"],
            "expected_task_revision": snapshot["task_revision"]})
        assert cancelled["status"] == "ok" and cancelled["data"]["state"] == "cancelled"


@pytest.mark.parametrize("remembered,allowed", [
    ({"memory_retention": "source_bound"}, False),
    ({"memory_retention": "long_term"}, True),
], indirect=["remembered"])
def test_plan_display_follows_memory_retention_when_only_original_sources_expire(remembered, allowed):
    runtime, profile, original, draft, _, _, _, reported = remembered
    memory_ref = reported["memory"]["items"][0]["memory_ref"]
    task = new_task(runtime, profile)
    searched = runtime.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache"})["data"]
    copied = copy.deepcopy(draft)
    copied.update(memory_search_refs=[searched["memory_search_ref"]], memory_refs=[memory_ref])
    saved = runtime.call("daeri_plan_submit", {"request_id": "memory-plan", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"], "draft": copied})
    assert saved["status"] == "ok", saved
    runtime.store.set_task_expiry(original["task_id"], (datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    result = runtime.call("daeri_plan_get", {"task_id": task["task_id"], "plan_id": saved["data"]["plan_id"]})
    assert result["status"] == ("ok" if allowed else "error"), result
    if not allowed:
        assert result["error"]["code"] == "ACCESS_DENIED" and result["data"] is None
