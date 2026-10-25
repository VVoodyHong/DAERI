"""Pure plan validation rejects malformed structure and invalid permissions."""

from copy import deepcopy

import pytest

from daeri.planning import validate_plan
from daeri.storage import StoreError


ROOT = {"segments": []}
NONE = {"categories": [], "source_fields": [], "context_sources": [], "retention": "none", "allow_model_recall": False}


class Registry:
    def get(self, profile, instance, capability):
        return {"contract_ref": "contract", "supported_purposes": ["collect"], "effects": {"changes_state": False},
                "scope_schema": {"type": "object"}, "budget_support": {"external_requests_enforceable": False}}


def inputs():
    policy = {"allowed_model_fields": [ROOT], "export_fields": [ROOT], "retained_fields": [ROOT], "masking": [], "memory": NONE}
    profile = {"resources": [{"instance_id": "resource", "policy": deepcopy(policy)}]}
    draft = {"purpose": "Read approved data", "steps": [{"step_id": "read", "description": "Read current resource", "purpose": "collect",
        "depends_on": [], "instance_id": "resource", "capability_id": "get", "contract_ref": "contract", "scope": {}, "input_bindings": []}],
        "budget": {"max_tool_calls": 1, "max_external_requests": None, "max_runtime_ms": 1000, "resource_limits": []},
        "output": {"format": "text", "destination": "conversation", "resource_rules": [{"instance_id": "resource", **deepcopy(policy)}],
                   "table_rules": [], "artifact_rules": []}, "memory_search_refs": ["search"], "memory_refs": [], "memory_limitations": [],
        "completion_criteria": [{"criterion_id": "read", "description": "Read approved source", "required": True, "step_ids": ["read"]}]}
    return profile, draft


def test_valid_typed_plan_preserves_engine_dictionary_contract():
    profile, draft = inputs()
    draft["output"].pop("table_rules")
    draft["output"].pop("artifact_rules")
    assert validate_plan(profile, draft, Registry()) == ["contract"]
    assert draft["steps"][0].get("conditions") is None


@pytest.mark.parametrize("field", ["table_rules", "artifact_rules"])
def test_new_plans_reject_legacy_result_generation_rules(field):
    profile, draft = inputs()
    draft["output"][field] = [{"legacy": "accepted only for existing request replay"}]
    with pytest.raises(StoreError, match="PLAN_INVALID"):
        validate_plan(profile, draft, Registry())


@pytest.mark.parametrize("defect", ["unknown_dependency", "mask_overlap", "selector_duplicate", "scope_above_ceiling"])
def test_semantic_errors_stay_plan_errors(defect):
    profile, draft = inputs()
    rule = draft["output"]["resource_rules"][0]
    if defect == "unknown_dependency":
        draft["steps"][0]["depends_on"] = ["missing"]
    elif defect == "mask_overlap":
        rule["masking"] = [{"field": ROOT, "action": "omit"}, {"field": {"segments": [{"key": "secret"}]}, "action": "redact"}]
    elif defect == "selector_duplicate":
        rule["allowed_model_fields"] = [ROOT, ROOT]
    else:
        profile["resources"][0]["policy"]["allowed_model_fields"] = []
    with pytest.raises(StoreError, match="PLAN_INVALID"):
        validate_plan(profile, draft, Registry(), [])
