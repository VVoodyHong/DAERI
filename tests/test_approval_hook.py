"""Approval guard operates on real conditional S3 state, without AWS requests."""

import importlib.util

import pytest

from test_cloud_s3 import ARN, S3, approval_wait, begin, context


def handler(event, store):
    assert importlib.util.find_spec('daeri.infrastructure.approval_hook') is not None
    from daeri.infrastructure.approval_hook import handle_approval_hook
    return handle_approval_hook(event, store)


def event(task, name='shell', kind='builtin', data=None):
    return {'hookEventId': 'hook-event', 'name': 'daeri_approval_guard', 'event': 'before_tool_call',
            'harnessArn': ARN, 'sessionId': task.session_id,
            'context': {'toolName': name, 'toolType': kind, 'toolUseId': 'tool-next',
                        'toolInput': {'command': 'pwd'} if data is None else data}}


def test_guard_blocks_all_execution_until_service_saved_actual_approval():
    client = S3()
    store, task, _ = begin(client)
    assert handler(event(task), store)['decision'] == 'deny'
    assert handler(event(task, 'confirm_plan', 'inline_function', {'plan': 'Inspect cache'}), store)['decision'] == 'allow'
    assert handler(event(task, 'confirm_plan', 'remote_mcp', {'plan': 'Inspect cache'}), store)['decision'] == 'deny'


def test_guard_allows_approved_live_session_and_revokes_when_model_requests_new_plan():
    client = S3()
    store, task = approval_wait(client)
    assert getattr(task, 'approval', None) is not None
    task = store.record_decision(task, 'plan-first', task.approval.plan, 'approve', '승인')
    results = ({'toolUseId': 'plan-first', 'status': 'success', 'content': [{'text': 'Approved'}]},)
    store.begin_turn(task, '', results)
    assert handler(event(task), store)['decision'] == 'allow'
    assert handler(event(task, 'confirm_plan', 'inline_function', {'plan': 'New scope'}), store)['decision'] == 'allow'
    assert handler(event(task), store)['decision'] == 'deny'


def test_invalid_replanning_input_still_withdraws_the_previous_execution_permission():
    store, task = approval_wait(S3())
    task = store.record_decision(task, 'plan-first', task.approval.plan, 'approve', '')
    result = ({'toolUseId': 'plan-first', 'status': 'success', 'content': [{'text': 'Approved'}]},)
    store.begin_turn(task, '', result)
    assert handler(event(task, 'confirm_plan', 'inline_function', {'plan': ''}), store)['decision'] == 'deny'
    assert handler(event(task), store)['decision'] == 'deny'


@pytest.mark.parametrize('change', [
    {'event': 'after_tool_call'}, {'sessionId': 'unknown'}, {'harnessArn': ARN + '-other'},
    {'context': {'truncated': True}},
    {'context': {'toolName': 'shell', 'toolType': 'builtin', 'toolUseId': 'tool-next',
                 'toolInput': {'truncated': True, 'preview': 'private'}}},
])
def test_invalid_unbound_or_truncated_hook_never_grants_execution(change):
    client = S3()
    store, task, _ = begin(client)
    assert handler({**event(task), **change}, store)['decision'] == 'deny'


@pytest.mark.parametrize('data', [{}, {'plan': ''}, {'plan': 'Inspect', 'approved': True}])
def test_confirmation_input_must_be_a_plan_without_model_supplied_decision(data):
    client = S3()
    store, task, _ = begin(client)
    assert handler(event(task, 'confirm_plan', 'inline_function', data), store)['decision'] == 'deny'


def test_storage_error_is_denied_without_leaking_provider_details():
    class BrokenStore:
        def hook_get(self, *_):
            raise RuntimeError('secret=PRIVATE')
    fake = type('Task', (), {'session_id': '01234567-89ab-cdef-0123-456789abcdef'})()
    result = handler(event(fake), BrokenStore())
    assert result['decision'] == 'deny' and 'PRIVATE' not in str(result)


def test_new_plan_is_not_allowed_if_previous_approval_cannot_be_revoked():
    client = S3()
    store, task = approval_wait(client)
    task = store.record_decision(task, 'plan-first', task.approval.plan, 'approve', '승인')
    results = ({'toolUseId': 'plan-first', 'status': 'success', 'content': [{'text': 'Approved'}]},)
    store.begin_turn(task, '', results)
    client.fail_manifest = 'conflict'
    assert handler(event(task, 'confirm_plan', 'inline_function', {'plan': 'New scope'}), store)['decision'] == 'deny'


@pytest.mark.parametrize('decision', ['revise', 'reject'])
def test_nonapproval_user_decision_keeps_execution_blocked(decision):
    client = S3()
    store, task = approval_wait(client)
    task = store.record_decision(task, 'plan-first', task.approval.plan, decision, '검토 응답')
    results = ({'toolUseId': 'plan-first', 'status': 'success', 'content': [{'text': decision}]},)
    store.begin_turn(task, '', results)
    assert handler(event(task), store)['decision'] == 'deny'


def test_lambda_entry_builds_service_store_lazily_with_one_sdk_attempt(monkeypatch):
    from daeri.infrastructure import approval_hook
    assert hasattr(approval_hook, 'lambda_handler')
    monkeypatch.setenv('DAERI_STORAGE_BUCKET', 'company-daeri')
    monkeypatch.setenv('DAERI_STORAGE_PREFIX', 'service/')
    calls = []
    class Client:
        closed = False
        def close(self):
            self.closed = True
    client = Client()
    import boto3
    def create_client(name, **options):
        calls.append((name, options))
        return client
    monkeypatch.setattr(boto3, 'client', create_client)
    monkeypatch.setattr(approval_hook, 'handle_approval_hook',
                        lambda event, store: {'decision': 'allow' if store.prefix == 'service/' else 'deny'})
    assert approval_hook.lambda_handler({}, None)['decision'] == 'allow'
    assert calls[0][0] == 's3'
    assert calls[0][1]['config'].retries['total_max_attempts'] == 1
    assert client.closed


def test_lambda_entry_without_storage_configuration_is_closed_failure(monkeypatch):
    from daeri.infrastructure import approval_hook
    assert hasattr(approval_hook, 'lambda_handler')
    monkeypatch.delenv('DAERI_STORAGE_BUCKET', raising=False)
    assert approval_hook.lambda_handler({}, None)['decision'] == 'deny'
