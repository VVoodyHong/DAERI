import copy

from test_execution_flow import approve
from test_memory_flow import knowledge_args, new_task, remembered


def test_invalidated_memory_cannot_be_read_through_an_old_report_or_replay(remembered):
    runtime, profile, _, original, _, _, _, _ = remembered
    update = runtime.call("daeri_memory_update", knowledge_args(remembered))
    memory_ref = update["data"]["memory_ref"]
    task = new_task(runtime, profile, "recall-report")
    search = runtime.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache 비교"})["data"]
    draft = copy.deepcopy(original)
    draft.update(steps=[], memory_search_refs=[search["memory_search_ref"]], memory_refs=[memory_ref],
        completion_criteria=[{"criterion_id": "recall", "description": "Summarize permitted prior knowledge", "required": True, "step_ids": []}])
    planned = runtime.call("daeri_plan_submit", {"request_id": "recall-plan", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"], "draft": draft})
    assert planned["status"] == "ok", planned
    saved = planned["data"]
    approved = approve(runtime, saved)
    args = {"request_id": "recall-report", "task_id": task["task_id"], "expected_task_revision": approved["task_revision"],
        "plan_id": saved["plan_id"], "plan_version": saved["plan_version"],
        'report': 'Use matching identifiers and verify the difference', 'evidence_refs': [memory_ref]}
    report = runtime.call("daeri_result_submit", args)
    assert report["status"] == "ok", report
    _, canonical = runtime.memory._row(profile["profile_id"], memory_ref)
    runtime.memory.invalidate(profile["profile_id"], memory_ref, reason="Confirmed correction",
                             source_records=canonical["_sources"])
    fetched = runtime.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": report["data"]["report_ref"]})
    assert fetched["status"] == "error", fetched
    replayed = runtime.call("daeri_result_submit", args)
    assert replayed["status"] == "error", replayed
