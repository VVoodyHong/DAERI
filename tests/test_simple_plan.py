"""The normal plan describes the job rather than runtime bookkeeping."""
from copy import deepcopy

import pytest
from daeri.core.contracts import INPUT_MODELS
from daeri.application.planning import resolve_plan_defaults, validate_plan
from daeri.core.errors import StoreError


class Registry:
    def get(self, profile, instance, capability):
        return {"contract_ref": "current", "supported_purposes": ["collect", "verify"],
            "effects": {"changes_state": False}, "scope_schema": {"type": "object"},
            "budget_support": {"external_requests_enforceable": False}}


def simple():
    return {"purpose": "연결된 자료를 조사한다", "steps": [
        {"step_id": "read", "description": "읽기 도구를 선택해 조사한다", "instance_id": "db"}],
        "output": {"format": "text"}, "completion_criteria": [
            {"criterion_id": "done", "description": "근거와 결과를 설명한다", "step_ids": ["read"]}]}


def test_plan_resolves_runtime_details_from_registered_connection():
    profile = {"resources": [{"instance_id": "db", "target": {}}]}
    original = simple()
    saved = resolve_plan_defaults(profile, original, Registry())
    assert original == simple()
    assert saved["steps"][0]["contract_ref"] == "current"
    assert saved["steps"][0]["capability_id"] == "*"
    assert saved["steps"][0]["scope"] == {}
    assert saved["budget"]["max_tool_calls"] is None
    assert saved["budget"]["max_runtime_ms"] is None
    assert saved["completion_criteria"][0]["required"] is True
    assert saved["output"]["destination"] == "conversation"
    assert saved["output"]["resource_rules"][0]["memory"]["retention"] == "long_term"
    assert validate_plan(profile, saved, Registry()) == ["current"]


def test_explicit_connection_policy_remains_effective_without_plan_policy_input():
    profile = {"resources": [{"instance_id": "db", "target": {}, "policy": {
        "allowed_model_fields": [], "export_fields": [], "retained_fields": [], "masking": []}}]}
    saved = resolve_plan_defaults(profile, simple(), Registry())
    rule = saved["output"]["resource_rules"][0]
    assert rule["allowed_model_fields"] == []
    assert rule["memory"]["source_fields"] == []


@pytest.mark.parametrize("defect", ["cycle", "unknown_resource", "write", "wrong_contract"])
def test_simple_plan_still_checks_actual_execution_boundary(defect):
    profile = {"resources": [{"instance_id": "db", "target": {}}]}
    draft = simple()
    step = draft["steps"][0]
    if defect == "cycle": step["depends_on"] = ["read"]
    elif defect == "unknown_resource": step["instance_id"] = "outside"
    elif defect == "write": step["purpose"] = "change"
    else: step["contract_ref"] = "stale"
    with pytest.raises(StoreError):
        validate_plan(profile, draft, Registry())


def test_model_tool_schemas_only_expose_job_inputs():
    hidden = {"request_id", "expected_task_revision", "contract_ref", "approval_id", "call_id",
              "plan_version", "approval_request_id", "stage", "previous_plan_id", "resource_rules",
              "max_external_requests", "resource_limits", "ttl_seconds", "department_id", "environment_id"}
    def visit(node):
        if isinstance(node, dict):
            assert not hidden.intersection(node.get("properties", {}))
            for value in node.values(): visit(value)
        elif isinstance(node, list):
            for value in node: visit(value)
    for model in INPUT_MODELS.values(): visit(model.model_json_schema())
