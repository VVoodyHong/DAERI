"""Input boundaries that keep task changes and approvals unambiguous."""

from copy import deepcopy
from importlib import import_module

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError


def validate(model_name, payload):
    return getattr(import_module("daeri.contracts"), model_name).model_validate(payload)


def approval(**changes):
    return {
        "request_id": "request-1",
        "task_id": "task-1",
        "expected_task_revision": 1,
        "plan_id": "plan-1",
        "plan_version": 1,
        "stage": "request",
        **changes,
    }


def plan():
    return {
        "purpose": "요청 자료를 확인한다",
        "steps": [],
        "budget": {
            "max_tool_calls": 0,
            "max_external_requests": None,
            "max_runtime_ms": 0,
            "resource_limits": [],
        },
        "output": {
            "format": "text",
            "destination": "현재 대화",
            "resource_rules": [],
            "table_rules": [],
            "artifact_rules": [],
        },
        "memory_search_refs": ["search-1"],
        "memory_refs": [],
        "memory_limitations": [],
        "completion_criteria": [
            {
                "criterion_id": "criterion-1",
                "description": "요청 자료를 근거로 설명한다",
                "required": True,
                "step_ids": [],
            }
        ],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("approval_request_id", "approval-1"),
        ("decision", "approve"),
        ("decision", None),
        ("user_response", "승인합니다"),
        ("user_message_ref", "message-1"),
    ],
)
def test_approval_request_forbids_all_response_fields(field, value):
    with pytest.raises(ValidationError):
        validate("ApprovalRequest", approval(**{field: value}))


@pytest.mark.parametrize("missing", ["approval_request_id", "decision", "user_response"])
def test_approval_response_requires_request_decision_and_actual_reply(missing):
    payload = approval(
        stage="respond", approval_request_id="approval-1", decision=None,
        user_response="조건을 다시 확인해 주세요",
    )
    del payload[missing]
    with pytest.raises(ValidationError):
        validate("ApprovalRequest", payload)


@pytest.mark.parametrize("decision", [None, "approve", "request_changes", "decline", "cancel"])
def test_approval_response_keeps_actual_reply_and_nullable_decision(decision):
    response = "  조건을 확인하고 승인합니다.\n"
    result = validate("ApprovalRequest", approval(
        stage="respond", approval_request_id="approval-1", decision=decision,
        user_response=response,
    ))
    assert result.user_response == response
    assert result.decision == decision
    assert "user_message_ref" not in result.model_dump(exclude_unset=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"decision": "yes"},
        {"user_response": ""},
        {"user_response": " \n\t"},
        {"user_message_ref": None},
        {"plan_version": True},
        {"expected_task_revision": "1"},
    ],
)
def test_approval_response_rejects_invalid_decisions_replies_and_versions(changes):
    payload = approval(
        stage="respond", approval_request_id="approval-1", decision="approve",
        user_response="승인합니다",
    )
    payload.update(changes)
    with pytest.raises(ValidationError):
        validate("ApprovalRequest", payload)


@pytest.mark.parametrize("changes", [{}, {"patch": {"question": "어느 환경인가요?"}, "action": "pause"}])
def test_task_update_requires_exactly_one_change_branch(changes):
    with pytest.raises(ValidationError):
        validate("TaskUpdate", {
            "request_id": "request-1", "task_id": "task-1",
            "expected_task_revision": 1, **changes,
        })


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"question": "어느 환경인가요?", "clarification": "운영"},
        {"question": "어느 환경인가요?", "context": {}},
        {"question": "어느 환경인가요?", "desired_output": {}},
    ],
)
def test_question_patch_is_nonempty_and_exclusive(patch):
    with pytest.raises(ValidationError):
        validate("TaskPatch", patch)


def test_task_update_accepts_pause_and_combined_clarification_fields():
    common = {"request_id": "request-1", "task_id": "task-1", "expected_task_revision": 1}
    assert validate("TaskUpdate", {**common, "action": "pause"}).action == "pause"
    result = validate("TaskUpdate", {
        **common, "patch": {
            "clarification": "등록된 환경을 사용해 주세요",
            "context": {"profile_id": "profile-1"},
            "desired_output": {"format": "text"},
        },
    })
    assert result.patch.context.profile_id == "profile-1"


@pytest.mark.parametrize("payload", [{}, {"task_id": "task-1", "profile_id": "profile-1"}])
def test_task_get_requires_one_lookup_target(payload):
    with pytest.raises(ValidationError):
        validate("TaskGet", payload)


@pytest.mark.parametrize("target", [{"task_id": "task-1"}, {"profile_id": "profile-1"}])
def test_task_get_preserves_lookup_branch_and_optional_pagination(target):
    assert validate("TaskGet", target).model_dump(exclude_unset=True) == target


@pytest.mark.parametrize("model_name,payload", [
    ("TaskCreate", {"request_id": "r", "request_text": "요청", "context": None}),
    ("ContextSelection", {"profile_id": None}),
    ("DesiredOutput", {"description": None}),
    ("TaskCancel", {"request_id": "r", "task_id": "t", "expected_task_revision": 1, "reason": None}),
    ("MemorySearch", {"task_id": "t", "query": "자료", "instance_ids": None}),
    ("PlanDraft", {**plan(), "previous_plan_id": None}),
])
def test_optional_fields_allow_omission_but_reject_explicit_null(model_name, payload):
    with pytest.raises(ValidationError):
        validate(model_name, payload)


@pytest.mark.parametrize("model_name,payload", [
    ("TaskCreate", {"request_id": "r", "request_text": "요청", "approved": True}),
    ("TaskCreate", {"request_id": "r", "request_text": "요청", "context": {"account": "other"}}),
    ("TaskCreate", {"request_id": "r", "request_text": "요청", "desired_output": {"execute": True}}),
    ("PlanDraft", {**plan(), "profile_id": "other"}),
    ("PlanBudget", {**plan()["budget"], "unlimited": True}),
    ("PlanOutput", {**plan()["output"], "path": "/tmp/result"}),
    ("ResourceList", {"task_id": "t", "profile_id": "other"}),
])
def test_input_models_reject_undeclared_fields_at_their_boundaries(model_name, payload):
    with pytest.raises(ValidationError):
        validate(model_name, payload)


@pytest.mark.parametrize("value", ["", " \n", 1, True])
def test_request_identifiers_and_text_reject_empty_or_coerced_values(value):
    with pytest.raises(ValidationError):
        validate("TaskCreate", {"request_id": value, "request_text": "요청"})
    with pytest.raises(ValidationError):
        validate("TaskCreate", {"request_id": "request-1", "request_text": value})


@pytest.mark.parametrize("model_name,payload", [
    ("TaskResume", {"request_id": "r", "task_id": "t", "expected_task_revision": True}),
    ("TaskCancel", {"request_id": "r", "task_id": "t", "expected_task_revision": 0}),
    ("TaskGet", {"task_id": "t", "limit": True}),
    ("TaskGet", {"task_id": "t", "limit": 101}),
    ("MemorySearch", {"task_id": "t", "query": "자료", "limit": "10"}),
    ("ResourceList", {"task_id": "t", "limit": 0}),
    ("PlanBudget", {**plan()["budget"], "max_tool_calls": True}),
    ("PlanBudget", {**plan()["budget"], "max_external_requests": -1}),
    ("PlanBudget", {**plan()["budget"], "max_runtime_ms": 1.0}),
])
def test_revisions_budgets_and_page_sizes_are_strict_bounded_integers(model_name, payload):
    with pytest.raises(ValidationError):
        validate(model_name, payload)


@pytest.mark.parametrize("model_name,payload", [
    ("MemorySearch", {"task_id": "t", "query": "자료", "instance_ids": ["db", "db"]}),
    ("MemorySearch", {"task_id": "t", "query": "자료", "instance_ids": []}),
    ("ResourceList", {"task_id": "t", "types": ["DB", "DB"]}),
    ("ResourceList", {"task_id": "t", "types": []}),
    ("PlanDraft", {**plan(), "memory_search_refs": ["search-1", "search-1"]}),
    ("PlanDraft", {**plan(), "memory_refs": ["memory-1", "memory-1"]}),
    ("PlanDraft", {**plan(), "carried_evidence_refs": ["result-1", "result-1"]}),
    ("CompletionCriterion", {"criterion_id": "c", "description": "확인", "required": True, "step_ids": ["s", "s"]}),
])
def test_identifier_lists_reject_duplicates_and_empty_resource_filter(model_name, payload):
    with pytest.raises(ValidationError):
        validate(model_name, payload)


def test_budget_rejects_duplicate_resource_metric_pairs():
    limit = {"instance_id": "db", "metric_id": "rows", "maximum": 10}
    with pytest.raises(ValidationError):
        validate("PlanBudget", {**plan()["budget"], "resource_limits": [limit, deepcopy(limit)]})
    result = validate("PlanBudget", {
        **plan()["budget"], "resource_limits": [limit, {**limit, "metric_id": "bytes"}],
    })
    assert [entry.metric_id for entry in result.resource_limits] == ["rows", "bytes"]


@pytest.mark.parametrize("maximum", [True, -1, float("nan"), float("inf"), -float("inf")])
def test_resource_limit_requires_a_finite_nonnegative_number(maximum):
    with pytest.raises(ValidationError):
        validate("PlanBudget", {
            **plan()["budget"], "resource_limits": [
                {"instance_id": "db", "metric_id": "rows", "maximum": maximum},
            ],
        })


@pytest.mark.parametrize("maximum", [9007199254740993, 1.25])
def test_resource_limit_preserves_the_requested_numeric_bound(maximum):
    result = validate("PlanBudget", {
        **plan()["budget"], "resource_limits": [
            {"instance_id": "db", "metric_id": "rows", "maximum": maximum},
        ],
    })
    assert result.resource_limits[0].maximum == maximum


@pytest.mark.parametrize("field", ["memory_search_refs", "completion_criteria"])
def test_plan_requires_search_evidence_and_completion_criteria(field):
    with pytest.raises(ValidationError):
        validate("PlanDraft", {**plan(), field: []})


def test_plan_rejects_duplicate_completion_ids_and_non_boolean_required():
    draft = plan()
    draft["completion_criteria"].append(deepcopy(draft["completion_criteria"][0]))
    with pytest.raises(ValidationError):
        validate("PlanDraft", draft)
    criterion = plan()["completion_criteria"][0]
    with pytest.raises(ValidationError):
        validate("CompletionCriterion", {**criterion, "required": 1})


@pytest.mark.parametrize("field", ["steps", "resource_rules", "table_rules", "artifact_rules"])
@pytest.mark.parametrize("invalid_value", [object(), float("nan"), float("inf")])
def test_deferred_plan_objects_still_reject_non_json_values(field, invalid_value):
    draft = plan()
    container = draft if field == "steps" else draft["output"]
    container[field] = [{"unimplemented": invalid_value}]
    with pytest.raises(ValidationError):
        validate("PlanDraft", draft)


def test_plan_submit_accepts_resource_free_plan_without_inventing_future_fields():
    result = validate("PlanSubmit", {
        "request_id": "request-1", "task_id": "task-1",
        "expected_task_revision": 1, "draft": plan(),
    })
    assert result.draft.steps == []
    assert result.draft.budget.max_external_requests is None
    assert result.draft.carried_evidence_refs == []
    assert "previous_plan_id" not in result.draft.model_dump(exclude_unset=True)


def assert_branch_contract(model_name, payload, accepted):
    model = getattr(import_module("daeri.contracts"), model_name)
    validator = Draft202012Validator(model.model_json_schema())
    assert validator.is_valid(payload) is accepted
    if accepted:
        model.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.parametrize("patch,accepted", [
    ({}, False),
    ({"question": "어느 환경인가요?"}, True),
    ({"question": "어느 환경인가요?", "clarification": "운영"}, False),
    ({"question": "어느 환경인가요?", "context": {}}, False),
    ({"question": "어느 환경인가요?", "desired_output": {}}, False),
    ({"clarification": "운영", "context": {}, "desired_output": {}}, True),
])
def test_patch_schema_advertises_a_nonempty_patch_and_exclusive_question(patch, accepted):
    assert_branch_contract("TaskPatch", patch, accepted)


@pytest.mark.parametrize("change,accepted", [
    ({}, False),
    ({"action": "pause"}, True),
    ({"patch": {"question": "어느 환경인가요?"}}, True),
    ({"patch": {}}, False),
    ({"patch": {"question": "어느 환경인가요?"}, "action": "pause"}, False),
    ({"patch": {"question": "어느 환경인가요?", "context": {}}}, False),
])
def test_update_schema_advertises_exactly_one_change_branch(change, accepted):
    assert_branch_contract("TaskUpdate", {
        "request_id": "r", "task_id": "t", "expected_task_revision": 1, **change,
    }, accepted)


@pytest.mark.parametrize("target,accepted", [
    ({}, False),
    ({"task_id": "t"}, True),
    ({"profile_id": "p"}, True),
    ({"task_id": "t", "profile_id": "p"}, False),
    ({"cursor": "c", "limit": 1}, False),
])
def test_lookup_schema_advertises_exactly_one_target(target, accepted):
    assert_branch_contract("TaskGet", target, accepted)


@pytest.mark.parametrize("extra,accepted", [
    ({}, True),
    ({"approval_request_id": "approval-1"}, False),
    ({"decision": "approve"}, False),
    ({"decision": None}, False),
    ({"user_response": "승인합니다"}, False),
    ({"user_message_ref": "message-1"}, False),
])
def test_request_schema_forbids_response_fields_even_when_nullable(extra, accepted):
    assert_branch_contract("ApprovalRequest", approval(**extra), accepted)


@pytest.mark.parametrize("decision", [None, "approve", "request_changes", "decline", "cancel"])
def test_response_schema_requires_actual_reply_and_allows_nullable_decision(decision):
    assert_branch_contract("ApprovalRequest", approval(
        stage="respond", approval_request_id="approval-1", decision=decision,
        user_response="실제 사용자 답변", user_message_ref="message-1",
    ), True)


@pytest.mark.parametrize("missing", ["approval_request_id", "decision", "user_response"])
def test_response_schema_requires_each_response_field(missing):
    payload = approval(
        stage="respond", approval_request_id="approval-1", decision=None,
        user_response="실제 사용자 답변",
    )
    del payload[missing]
    assert_branch_contract("ApprovalRequest", payload, False)


def detailed_plan():
    value = plan()
    value["steps"] = [{"step_id": "read", "description": "Read current resource", "purpose": "collect",
        "depends_on": [], "instance_id": "resource", "capability_id": "get", "contract_ref": "contract",
        "scope": {}, "input_bindings": []}]
    value["output"]["resource_rules"] = [{"instance_id": "resource", "allowed_model_fields": [{"segments": []}],
        "export_fields": [], "retained_fields": [], "masking": [], "memory": {"categories": [], "source_fields": [],
        "context_sources": [], "retention": "none", "allow_model_recall": False}}]
    value["output"]["table_rules"] = [{"table_id": "table", "sources": [{"source_id": "rows", "step_id": "read", "rows_pointer": "", "key_pointers": []}],
        "join_mode": "none", "coverage": "selected", "allowed_operations": ["copy"], "max_rows": 10, "ratio_scale": None}]
    value["output"]["artifact_rules"] = [{"artifact_id": "file", "format_id": "json", "format_contract_ref": "format-json", "input_kind": "json", "options": {}, "max_bytes": 100}]
    return value


@pytest.mark.parametrize("path,value", [
    (("steps", 0, "depends_on"), [None]),
    (("steps", 0, "scope"), []),
    (("steps", 0, "input_bindings"), [None]),
    (("steps", 0, "conditions"), [{"binding_id": "b", "operator": "eq"}]),
    (("output", "resource_rules", 0, "allowed_model_fields"), [{"segments": [{"key": "x", "index": 1}]}]),
    (("output", "resource_rules", 0, "masking"), [{"field": {"segments": []}, "action": "execute"}]),
    (("output", "resource_rules", 0, "memory", "allow_model_recall"), 1),
    (("output", "table_rules", 0, "sources"), [None]),
    (("output", "table_rules", 0, "sources", 0, "rows_pointer"), "$.rows"),
    (("output", "table_rules", 0, "max_rows"), True),
    (("output", "artifact_rules", 0, "options"), None),
    (("output", "artifact_rules", 0, "max_bytes"), "100"),
])
def test_nested_plan_contract_rejects_bad_shape_in_runtime_and_schema(path, value):
    draft = detailed_plan()
    parent = draft
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    assert_branch_contract("PlanDraft", draft, False)


@pytest.mark.parametrize("segment,accepted", [
    ({"key": ""}, True), ({"index": 0}, True), ({"each": True}, True),
    ({"each": False}, False), ({"each": 1}, False), ({"index": True}, False),
    ({"key": "x", "extra": 1}, False), ({}, False),
])
def test_selector_segments_advertise_exactly_one_strict_shape(segment, accepted):
    assert_branch_contract("FieldSelector", {"segments": [segment]}, accepted)


@pytest.mark.parametrize("plan_id,plan_version,accepted", [(None, None, True), ("p", 1, True), ("p", None, False), (None, 1, False)])
def test_result_submit_schema_advertises_nullable_plan_pair(plan_id, plan_version, accepted):
    assert_branch_contract("ResultSubmit", {"request_id": "r", "task_id": "t", "expected_task_revision": 1,
        "plan_id": plan_id, "plan_version": plan_version, "report": {}}, accepted)


@pytest.mark.parametrize("operation,fields,accepted", [
    ("store", {}, False), ("invalidate", {}, False), ("invalidate", {"memory_ref": "memory"}, True),
    ("invalidate", {"memory_ref": "memory", "draft": {}}, False),
])
def test_memory_update_schema_advertises_operation_fields(operation, fields, accepted):
    assert_branch_contract("MemoryUpdate", {"request_id": "r", "task_id": "t", "expected_task_revision": 1,
        "operation": operation, "source_refs": ["source"], "reason": "verified", **fields}, accepted)


def test_public_limits_match_service_and_positive_byte_contract():
    assert_branch_contract("ResultGet", {"task_id": "t", "record_ref": "r", "limit_bytes": 1}, True)
    assert_branch_contract("MemorySearch", {"task_id": "t", "query": "x", "limit": 51}, False)


def test_duplicate_selectors_match_schema_and_runtime_rejection():
    draft = detailed_plan()
    draft["output"]["resource_rules"][0]["allowed_model_fields"] = [{"segments": []}, {"segments": []}]
    assert_branch_contract("PlanDraft", draft, False)


@pytest.mark.parametrize("operator,fields,accepted", [
    ("exists", {}, True), ("exists", {"value": None}, False),
    ("eq", {"value": None}, True), ("eq", {}, False), ("eq", {"value": [1]}, False),
    ("in", {"value": [None, 1, "x", True]}, True), ("in", {"value": []}, False),
    ("in", {"value": [1, {}]}, False), ("eq", {"value": {}}, False),
])
def test_condition_schema_matches_scalar_comparison_contract(operator, fields, accepted):
    assert_branch_contract("StepCondition", {"binding_id": "b", "operator": operator, **fields}, accepted)


@pytest.mark.parametrize("source,target,accepted", [("", None, True), ("/rows/0/a~1b", "/arg", True), ("$.rows", "/arg", False), ("/bad~2", "/arg", False), ("/data", "", False)])
def test_binding_pointer_schema_preserves_root_source_and_nonempty_target(source, target, accepted):
    assert_branch_contract("InputBinding", {"binding_id": "b", "source_step_id": "s", "source_pointer": source, "target_pointer": target}, accepted)
