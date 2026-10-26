"""Local source registration and host observations share the approved work flow."""

import shutil
import os

import pytest

from daeri.application.runtime import Runtime
from daeri.core.errors import StoreError
from daeri.infrastructure.resources import ResourceRegistry
from daeri.infrastructure.storage import Store


def local_resource(path, instance="source"):
    return {"instance_id": instance, "type": "Source", "adapter": "local_source",
            "target": {"path": str(path)}, "identity_ref": "local-user",
            "connection": {"transport": "host"},
            "capabilities": [{"capability_id": "source-observe", "tool_name": "source_observe",
                              "constraints": {}}]}


@pytest.fixture
def local_registry(tmp_path):
    root = tmp_path / "source tree"
    root.mkdir()
    (root / "app.py").write_text("print('hello')\n")
    store = Store(tmp_path / "data")
    try:
        profile = store.put_profile("local", resources=[local_resource(root)],
                                    embedding={"provider": "keyword_only"})
        registry = ResourceRegistry(store)
        yield root, store, profile, registry
    finally:
        store.close()


def test_local_source_diagnosis_and_observation_use_no_mcp(local_registry, monkeypatch):
    root, store, profile, registry = local_registry

    async def unexpected(*args, **kwargs):
        pytest.fail("Local source must use the host, not launch an MCP process")

    monkeypatch.setattr(registry, "_session", unexpected)
    assert registry.diagnose(profile, "source")["capability_count"] == 1
    listed = registry.list(profile)[0]
    assert listed["target"] == {"path": str(root)}
    invocation = registry.normalize(profile, "source", "source-observe",
                                    {"summary": "app.py prints a greeting", "paths": ["app.py"]}, {})
    result = registry.execute(invocation)
    assert result["result"] == {"path": str(root), "summary": "app.py prints a greeting",
                                "paths": ["app.py"], "observation_source": "host"}
    assert result["external_requests"] == 0
    assert result["source_complete"] is None


@pytest.mark.parametrize("path,code", [("../outside", "SCOPE_VIOLATION"),
                                        ("missing.py", "NOT_FOUND")])
def test_local_observation_rechecks_paths(local_registry, path, code):
    _, _, profile, registry = local_registry
    registry.diagnose(profile, "source")
    with pytest.raises(StoreError) as error:
        invocation = registry.normalize(profile, "source", "source-observe",
                                        {"summary": "Observed", "paths": [path]}, {})
        registry.execute(invocation)
    assert error.value.code == code


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_local_source_checks_symlink_target_at_execution(local_registry, tmp_path):
    root, _, profile, registry = local_registry
    outside = tmp_path / "outside.py"
    outside.write_text("outside")
    registry.diagnose(profile, "source")
    invocation = registry.normalize(profile, "source", "source-observe",
                                    {"summary": "Observed", "paths": ["app.py"]}, {})
    (root / "app.py").unlink()
    (root / "app.py").symlink_to(outside)
    with pytest.raises(StoreError) as error:
        registry.execute(invocation)
    assert error.value.code == "SCOPE_VIOLATION"


def test_local_source_unavailable_after_diagnosis_is_actionable(local_registry):
    root, store, profile, registry = local_registry
    registry.diagnose(profile, "source")
    invocation = registry.normalize(profile, "source", "source-observe", {"summary": "Observed"}, {})
    shutil.rmtree(root)
    assert store.get_profile("local")["name"] == "local"
    with pytest.raises(StoreError):
        registry.execute(invocation)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_local_source_root_retargeting_requires_a_new_binding(local_registry, tmp_path):
    root, _, profile, registry = local_registry
    registry.diagnose(profile, "source")
    invocation = registry.normalize(profile, "source", "source-observe", {"summary": "Observed"}, {})
    moved = tmp_path / "another-project"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(StoreError):
        registry.execute(invocation)


def test_profile_delete_keeps_local_project_files(local_registry):
    from daeri.config.setup import delete_profile, preview_profile_deletion

    root, store, _, registry = local_registry
    registry.diagnose(store.get_profile("local"), "source")
    preview = preview_profile_deletion(store, "local")
    assert delete_profile(store, "local", preview["deletion_token"], credentials=None)["deleted"]
    assert (root / "app.py").read_text() == "print('hello')\n"
    assert store.list_profiles() == []


def test_local_observation_follows_approval_and_can_complete_work(local_registry, request):
    _, store, profile, registry = local_registry
    registry.diagnose(profile, "source")
    runtime = Runtime(store.data_dir)
    request.addfinalizer(runtime.close)
    created = runtime.call("daeri_task_create", {"request_text": "Explain the source entry point",
        "context": {"profile_id": profile["profile_id"]}})
    assert created["status"] == "ok", created
    task_id = created["data"]["task_id"]
    plan = runtime.call("daeri_plan_submit", {"task_id": task_id, "draft": {
        "purpose": "Explain the entry point", "steps": [{"step_id": "inspect",
            "description": "Inspect source using host tools and register observations",
            "instance_id": "source", "capability_id": "source-observe"}],
        "output": {"format": "text"}, "completion_criteria": [{"criterion_id": "explained",
            "description": "Entry point inspected", "step_ids": ["inspect"]}]}})
    assert plan["status"] == "ok", plan
    arguments = {"task_id": task_id, "step_id": "inspect",
                 "parameters": {"summary": "app.py prints a greeting", "paths": ["app.py"]}}
    assert runtime.call("daeri_capability_execute", arguments)["status"] == "error"
    approved = runtime.call("daeri_approval_request", {"task_id": task_id,
        "plan_id": plan["data"]["plan_id"], "decision": "approve", "user_response": "진행해"})
    assert approved["status"] == "ok", approved
    observed = runtime.call("daeri_capability_execute", arguments)
    assert observed["status"] == "ok", observed
    reference = observed["data"]["result_ref"]
    submitted = runtime.call("daeri_result_submit", {"task_id": task_id, "report": {
        "summary": "Entry point explained", "findings": [{"kind": "observation",
            "text": "app.py prints a greeting", "evidence_refs": [reference]}],
        "checks": [{"criterion_id": "explained", "status": "met",
            "method": "Host inspected app.py", "evidence_refs": [reference]}],
        "artifacts": [], "limitations": [], "next_actions": [], "uncertain_effects": [],
        "proposed_task_state": "succeeded"}, "memory_candidates": [{"draft": {
            "category": "resource_structure", "statement": "app.py prints a greeting",
            "applicability": "This local project", "instance_ids": ["source"],
            "verification_method": "Inspect the current app.py using host tools"},
            "source_refs": [reference]}]})
    assert submitted["status"] == "ok", submitted
    assert submitted["data"]["task_state"] == "succeeded"
    assert submitted["data"]["memory"]["state"] == "ready"
    remembered = runtime.memory.get(profile["profile_id"],
        submitted["data"]["memory"]["items"][0]["memory_ref"])
    assert remembered["statement"] == "app.py prints a greeting"
