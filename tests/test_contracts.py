"""Public MCP inputs express the job; runtime metadata stays internal."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from daeri.core.contracts import INPUT_MODELS
import daeri.core.contracts as contracts


def check(name, value, accepted=True):
    model = getattr(contracts, name)
    assert Draft202012Validator(model.model_json_schema()).is_valid(value) is accepted
    if accepted:
        return model.model_validate(value)
    with pytest.raises(ValidationError):
        model.model_validate(value)


@pytest.mark.parametrize('name,model', INPUT_MODELS.items())
def test_host_schemas_have_an_object_root_and_no_runtime_bookkeeping(name, model):
    schema = model.model_json_schema()
    Draft202012Validator.check_schema(schema)
    assert schema['type'] == 'object'
    assert not {'oneOf', 'anyOf', 'allOf'} & schema.keys()
    assert not {'request_id', 'expected_task_revision', 'call_id', 'contract_ref',
        'approval_id', 'approval_request_id', 'plan_version', 'stage'} & schema['properties'].keys()


@pytest.mark.parametrize('name,value', [
    ('TaskCreate', {'request_text': '업무'}),
    ('TaskUpdate', {'task_id': 't', 'patch': {'clarification': '운영 환경'}}),
    ('TaskResume', {'task_id': 't'}), ('TaskCancel', {'task_id': 't'}),
    ('CapabilityExecute', {'task_id': 't', 'step_id': 'read', 'parameters': {}}),
    ('ApprovalRequest', {'task_id': 't', 'plan_id': 'p', 'decision': 'approve', 'user_response': '진행해'}),
    ('ResultSubmit', {'task_id': 't', 'report': {}}),
])
def test_normal_calls_require_only_job_inputs(name, value):
    check(name, value)


@pytest.mark.parametrize('value', ['', ' \n', 1, True, None])
def test_task_text_is_a_nonempty_strict_string(value):
    check('TaskCreate', {'request_text': value}, False)


@pytest.mark.parametrize('patch,accepted', [
    ({}, False), ({'question': '어느 환경인가요?'}, True),
    ({'question': '어느 환경인가요?', 'clarification': '운영'}, False),
    ({'clarification': '운영', 'context': {}, 'desired_output': {}}, True),
])
def test_patch_has_one_question_or_changes(patch, accepted):
    check('TaskPatch', patch, accepted)


@pytest.mark.parametrize('change,accepted', [
    ({}, False), ({'action': 'pause'}, True),
    ({'patch': {'question': '어느 환경인가요?'}}, True),
    ({'patch': {}}, False), ({'patch': {'clarification': '운영'}, 'action': 'pause'}, False),
])
def test_update_has_exactly_one_change_branch(change, accepted):
    check('TaskUpdate', {'task_id': 't', **change}, accepted)


@pytest.mark.parametrize('target,accepted', [
    ({}, False), ({'task_id': 't'}, True), ({'profile_id': 'p'}, True),
    ({'task_id': 't', 'profile_id': 'p'}, False),
])
def test_lookup_requires_one_target(target, accepted):
    check('TaskGet', target, accepted)


@pytest.mark.parametrize('decision', [None, 'approve', 'request_changes', 'decline', 'cancel'])
def test_approval_always_requires_the_actual_user_reply(decision):
    value = {'task_id': 't', 'plan_id': 'p', 'decision': decision, 'user_response': '실제 사용자 답변'}
    check('ApprovalRequest', value)
    for field in ('decision', 'user_response', 'plan_id'):
        missing = value.copy()
        del missing[field]
        check('ApprovalRequest', missing, False)


def plan():
    return {'purpose': '요청을 확인한다', 'steps': [], 'output': {'format': 'text'},
        'completion_criteria': [{'criterion_id': 'done', 'description': '근거를 설명한다', 'step_ids': []}]}


def test_minimal_plan_has_no_field_permissions_or_required_budget():
    value = check('PlanDraft', plan())
    assert value.budget.max_tool_calls is None
    assert value.budget.max_runtime_ms is None
    assert value.memory_search_refs == []
    assert value.output.destination == 'conversation'
    assert value.completion_criteria[0].required is True


@pytest.mark.parametrize('field,value', [('memory_refs', ['m', 'm'])])
def test_plan_completion_and_evidence_ids_are_validated(field, value):
    check('PlanDraft', {**plan(), field: value}, False)


def test_plan_can_leave_presentation_and_completion_to_the_host():
    value = check('PlanDraft', {'purpose': '사용자 질문에 답한다', 'steps': []})
    assert value.output.format == 'user_request'
    assert value.completion_criteria == []
    check('PlanDraft', {**plan(), 'completion_criteria': []})


@pytest.mark.parametrize('content', ['# 결과\n자유 형식 보고', {'outcome': {'table': [1, 2]}}, [1, '완료'], 7, True, None])
def test_report_content_is_free_json_and_bookkeeping_is_separate(content):
    value = check('ResultSubmit', {'task_id': 't', 'report': content})
    assert value.report == content
    assert value.state == 'succeeded'
    assert value.evidence_refs == value.artifacts == []


def test_file_metadata_is_visible_in_the_report_tool_schema():
    artifact = {'artifact_id': 'answer', 'label': '답변', 'local_path': '/workspace/answer.xlsx',
                'media_type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
    value = check('ResultSubmit', {'task_id': 't', 'report': '완료', 'artifacts': [artifact]})
    assert value.artifacts[0].source_refs == []
    check('ResultSubmit', {'task_id': 't', 'report': '완료', 'artifacts': [
        {'kind': 'existing', 'artifact_id': 'prior', 'label': '기존 결과', 'artifact_ref': 'record'}]})
    for field in ('local_path', 'media_type'):
        incomplete = artifact.copy()
        del incomplete[field]
        check('ResultSubmit', {'task_id': 't', 'report': '완료', 'artifacts': [incomplete]}, False)


def test_bad_memory_candidate_is_deferred_to_memory_processing():
    check('ResultSubmit', {'task_id': 't', 'report': '완료', 'memory_candidates': ['unfinished candidate']})


@pytest.mark.parametrize('content', ['자유 서술', {'table': [[1, 2]], 'meaning': '팀별 현황'}, None])
def test_subagent_completion_accepts_free_content_with_separate_sources(content):
    value = check('SubagentRequest', {'operation': 'complete', 'task_id': 't',
        'delegation_token': 'token', 'result': content, 'evidence_refs': ['ref']})
    assert value.result == content
    assert value.evidence_refs == ['ref']


@pytest.mark.parametrize('value', [True, -1, '10'])
def test_optional_budget_limits_are_strict_integers(value):
    check('PlanBudget', {'max_tool_calls': value}, False)


@pytest.mark.parametrize('name,value', [
    ('TaskCreate', {'request_text': '업무', 'approved': True}),
    ('ContextSelection', {'profile_id': None}),
    ('MemorySearch', {'task_id': 't', 'query': '기억', 'limit': 51}),
    ('MemorySearch', {'task_id': 't', 'query': '기억', 'instance_ids': ['db', 'db']}),
    ('PlanOutput', {'format': 'text', 'table_rules': []}),
    ('PlanStep', {'step_id': 's', 'description': '변경', 'instance_id': 'db', 'purpose': 'change'}),
    ('CapabilityExecute', {'task_id': 't', 'step_id': 's', 'parameters': [], 'reconcile_task_id': 'x'}),
])
def test_trust_boundary_rejects_bad_types_unknown_and_unsupported_fields(name, value):
    check(name, value, False)


@pytest.mark.parametrize('segment,accepted', [
    ({'key': ''}, True), ({'index': 0}, True), ({'each': True}, True),
    ({'each': False}, False), ({'each': 1}, False), ({'index': True}, False),
    ({'key': 'x', 'extra': 1}, False), ({}, False),
])
def test_field_selector_shape_remains_strict_for_saved_explicit_policies(segment, accepted):
    check('FieldSelector', {'segments': [segment]}, accepted)


@pytest.mark.parametrize('operation,fields,required,unrelated', [
    ('create', {'assignment': {'label': '확인', 'objective': '자료 확인', 'step_ids': ['s'],
        'completion_criteria': ['근거 확인']}}, 'assignment', {'delegation_token': 'token'}),
    ('get', {'delegation_token': 'token'}, 'delegation_token', {'query': '자료'}),
    ('execute', {'delegation_token': 'token', 'call': {'step_id': 's', 'parameters': {}}}, 'call', {'query': '자료'}),
    ('search', {'delegation_token': 'token', 'query': '자료'}, 'query', {'record_ref': 'ref'}),
    ('read', {'delegation_token': 'token', 'record_ref': 'ref'}, 'record_ref', {'query': '자료'}),
    ('complete', {'delegation_token': 'token', 'result': {'summary': '확인', 'evidence_refs': [], 'limitations': []}},
        'result', {'query': '자료'}),
    ('revoke', {'delegation_id': 'd'}, 'delegation_id', {'delegation_token': 'token'}),
])
def test_subagent_operations_require_only_their_own_job_inputs(operation, fields, required, unrelated):
    payload = {'operation': operation, 'task_id': 't', **fields}
    check('SubagentRequest', payload)
    missing = payload.copy()
    del missing[required]
    check('SubagentRequest', missing, False)
    check('SubagentRequest', {**payload, **unrelated}, False)


def test_old_saved_callers_can_still_validate_internal_keys_without_advertising_them():
    value = contracts.TaskCancel.model_validate({'task_id': 't', 'request_id': 'old', 'expected_task_revision': 1})
    assert value.request_id == 'old'
    with pytest.raises(ValidationError):
        contracts.TaskCancel.model_validate({'task_id': 't', 'expected_task_revision': True})


def test_python_runtime_rejects_float_even_when_json_schema_treats_it_as_integer():
    with pytest.raises(ValidationError):
        contracts.PlanBudget.model_validate({'max_tool_calls': 1.0})
