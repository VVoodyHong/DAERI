import copy

from test_execution_flow import approve
from test_memory_flow import knowledge_args, new_task, remembered


def test_invalidated_memory_cannot_be_read_through_an_old_report_or_replay(remembered):
    engine, profile, _, original, _, _, _, _ = remembered
    update = engine.call("daeri_memory_update", knowledge_args(remembered))
    memory_ref = update["data"]["memory_ref"]
    task = new_task(engine, profile, "recall-report")
    search = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "Cache 비교"})["data"]
    draft = copy.deepcopy(original)
    draft.update(steps=[], memory_search_refs=[search["memory_search_ref"]], memory_refs=[memory_ref],
        completion_criteria=[{"criterion_id": "recall", "description": "Summarize permitted prior knowledge", "required": True, "step_ids": []}])
    draft["output"].update(table_rules=[], artifact_rules=[])
    planned = engine.call("daeri_plan_submit", {"request_id": "recall-plan", "task_id": task["task_id"],
        "expected_task_revision": task["task_revision"], "draft": draft})
    assert planned["status"] == "ok", planned
    saved = planned["data"]
    approved = approve(engine, saved)
    args = {"request_id": "recall-report", "task_id": task["task_id"], "expected_task_revision": approved["task_revision"],
        "plan_id": saved["plan_id"], "plan_version": saved["plan_version"], "report": {
            "summary": "Previously confirmed Cache comparison procedure", "proposed_task_state": "succeeded",
            "findings": [{"kind": "memory", "text": "Use matching identifiers and verify the difference", "evidence_refs": [memory_ref]}],
            "tables": [], "checks": [{"criterion_id": "recall", "status": "met", "method": "Read permitted prior knowledge", "evidence_refs": [memory_ref]}],
            "limitations": [], "uncertain_effects": [], "artifacts": [], "next_actions": []}}
    report = engine.call("daeri_result_submit", args)
    assert report["status"] == "ok", report
    _, canonical = engine.memory._row(profile["profile_id"], memory_ref)
    engine.memory.invalidate(profile["profile_id"], memory_ref, reason="Confirmed correction",
                             source_records=canonical["_sources"])
    fetched = engine.call("daeri_result_get", {"task_id": task["task_id"], "record_ref": report["data"]["report_ref"]})
    assert fetched["status"] == "error", fetched
    replayed = engine.call("daeri_result_submit", args)
    assert replayed["status"] == "error", replayed
