"""Explicit host handoff preserves approval and waits for committed live reads."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import threading

import pytest

from daeri.application.runtime import Runtime
from daeri.infrastructure.storage import Store
from test_execution_flow import configured, begin, approve
from test_mcp_server import connect, envelope
from test_subagents import create_subagent, subagent_call


def test_live_mcp_process_handoff_preserves_approval_and_fences_the_previous_host(tmp_path):
    directory = tmp_path / 'data'
    with closing(Store(directory)) as store:
        profile = store.put_profile('shared', embedding={'provider': 'keyword_only'})

    async def scenario():
        async with connect(directory) as (previous, _), connect(directory) as (current, _):
            async def call(session, tool, args):
                return envelope(await session.call_tool(tool, args))

            created = await call(previous, 'daeri_task_create', {'request_text': 'Shared local review',
                'context': {'profile_id': profile['profile_id']}})
            task = created['data']['task_id']
            planned = await call(previous, 'daeri_plan_submit', {'task_id': task,
                'draft': {'purpose': 'Review the saved request', 'steps': []}})
            plan = planned['data']['plan_id']
            approved = await call(previous, 'daeri_approval_request', {'task_id': task, 'plan_id': plan,
                'decision': 'approve', 'user_response': 'Approve this synthetic review'})
            for args in ({'task_id': task}, {'task_id': task, 'handoff': False}):
                ordinary = await call(current, 'daeri_task_resume', args)
                assert ordinary['error']['code'] == 'OWNER_CONFLICT', ordinary
            handed = await call(current, 'daeri_task_resume', {'task_id': task, 'handoff': True})
            assert handed['status'] == 'ok', handed
            assert handed['data']['resumed'] is True
            assert handed['data']['active_plan_ref']['plan_id'] == plan
            saved = await call(current, 'daeri_plan_get', {'task_id': task, 'plan_id': plan})
            assert saved['data']['plan']['approval_request']['approval_id'] == approved['data']['approval_id']
            rejected = await call(previous, 'daeri_result_submit', {'task_id': task, 'report': 'Old host'})
            assert rejected['error']['code'] == 'OWNER_CONFLICT', rejected
            completed = await call(current, 'daeri_result_submit', {'task_id': task, 'report': 'New host'})
            assert completed['status'] == 'ok', completed
            assert completed['data']['task_state'] == 'succeeded'

    asyncio.run(scenario())


def test_idle_handoff_keeps_evidence_and_usage_and_revokes_old_delegations(configured):
    previous, profile = configured
    saved, _ = begin(previous, profile)
    approved = approve(previous, saved)
    task_id = saved['task_id']
    observed = previous.call('daeri_capability_execute', {
        'task_id': task_id, 'step_id': 'read-0', 'parameters': {'key': 'orders'}})
    assert observed['status'] == 'ok', observed
    grant = create_subagent(previous, saved, ['read-0'])['data']
    before = previous.store.get_task(task_id)
    with closing(Runtime(previous.store.data_dir)) as current:
        args = {'request_id': 'handoff', 'task_id': task_id, 'handoff': True}
        handed = current.call('daeri_task_resume', args)
        assert handed['status'] == 'ok', handed
        after = current.store.get_task(task_id)
        assert after['owner_id'] == current.owner
        assert after['plans'] == before['plans']
        assert after['approvals'] == before['approvals']
        assert after['usage'] == before['usage']
        assert approve(previous, saved)['approval_id'] == approved['approval_id']
        assert previous.call('daeri_task_get', {'task_id': task_id})[
            'data']['task']['execution_owner_status'] == 'other'
        assert current.call('daeri_result_get', {'task_id': task_id,
            'record_ref': observed['data']['result_ref']})['status'] == 'ok'
        assert subagent_call(previous, saved, grant, 'get')['error']['code'] == 'SUBAGENT_UNAVAILABLE'
        assert previous.call('daeri_capability_execute', {'task_id': task_id, 'step_id': 'read-0',
            'parameters': {'key': 'orders'}})['error']['code'] == 'OWNER_CONFLICT'
        continued = current.call('daeri_capability_execute', {'task_id': task_id, 'step_id': 'read-1',
            'parameters': {'key': 'orders'}})
        assert continued['status'] == 'ok', continued
        assert current.store.get_task(task_id)['usage']['tool_calls'] == 2
        assert current.call('daeri_plan_get', {'task_id': task_id, 'plan_id': saved['plan_id']})[
            'data']['plan']['approval_request']['approval_id'] == approved['approval_id']
        previous.call('daeri_task_resume', {'task_id': task_id, 'handoff': True})
        replay = current.call('daeri_task_resume', args)
        assert replay['data']['resumed'] is False, replay
        assert current.store.get_task(task_id)['owner_id'] == previous.owner


@pytest.mark.parametrize('delegated', [False, True])
@pytest.mark.parametrize('record_state', ['pending', 'unknown'])
def test_handoff_waits_for_live_reads_then_the_same_request_can_claim_ownership(configured, monkeypatch,
                                                                              delegated, record_state):
    previous, profile = configured
    saved, _ = begin(previous, profile)
    approve(previous, saved)
    task_id = saved['task_id']
    grant = create_subagent(previous, saved, ['read-0'])['data'] if delegated else None
    with closing(Runtime(previous.store.data_dir)) as current, closing(Runtime(previous.store.data_dir)) as executor:
        reader = executor if delegated else previous
        real_execute = reader.registry.execute
        started, release = threading.Event(), threading.Event()

        def waiting(invocation, **options):
            started.set()
            assert release.wait(timeout=5)
            return real_execute(invocation, **options)

        monkeypatch.setattr(reader.registry, 'execute', waiting)
        args = {'request_id': 'waiting-handoff', 'task_id': task_id, 'handoff': True}
        call = {'task_id': task_id, 'step_id': 'read-0', 'parameters': {'key': 'orders'}}
        if delegated:
            call = {'task_id': task_id, 'operation': 'execute', 'delegation_token': grant['delegation_token'],
                    'call': {'step_id': 'read-0', 'parameters': {'key': 'orders'}}}
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(reader.call, 'daeri_subagent' if delegated else 'daeri_capability_execute', call)
            try:
                assert started.wait(timeout=5)
                if record_state == 'unknown':
                    with previous.store.transaction():
                        task = previous.store.get_task(task_id)
                        task['calls'][0]['call_state'] = 'unknown'
                        previous._save(task)
                before = current.store.get_task(task_id)
                handed = current.call('daeri_task_resume', args)
                assert handed['status'] == 'pending', handed
                assert handed['data']['resumed'] is False
                assert handed['data']['unresolved_call_ids'] == [before['calls'][0]['call_id']]
                assert current.store.get_task(task_id) == before
            finally:
                release.set()
            observed = future.result(timeout=10)
        assert observed['status'] == 'ok', observed
        handed = current.call('daeri_task_resume', args)
        assert handed['status'] == 'ok', handed
        assert handed['data']['resumed'] is True
        after = current.store.get_task(task_id)
        assert after['owner_id'] == current.owner
        assert after['calls'][0]['call_state'] == 'completed'
        assert after['calls'][0]['result_ref'] == observed['data']['result_ref']
        assert after['reserved']['runtime_ms'] == 0
        assert after['usage']['tool_calls'] == 1


def test_handoff_of_a_user_paused_task_still_requires_new_approval(configured):
    previous, profile = configured
    saved, _ = begin(previous, profile)
    approved = approve(previous, saved)
    task_id = saved['task_id']
    previous.call('daeri_task_update', {'task_id': task_id, 'action': 'pause'})
    with closing(Runtime(previous.store.data_dir)) as current:
        handed = current.call('daeri_task_resume', {'task_id': task_id, 'handoff': True})
        assert handed['status'] == 'pending', handed
        assert handed['data']['state'] == 'awaiting_approval'
        assert handed['data']['resumed'] is False
        blocked = current.call('daeri_capability_execute', {'task_id': task_id,
            'step_id': 'read-0', 'parameters': {'key': 'orders'}})
        assert blocked['error']['code'] == 'TASK_STATE_INVALID', blocked
        renewed = current.call('daeri_approval_request', {'task_id': task_id, 'plan_id': saved['plan_id'],
            'decision': 'approve', 'user_response': 'Resume the paused work here'})
        assert renewed['status'] == 'ok', renewed
        assert renewed['data']['approval_id'] != approved['approval_id']
