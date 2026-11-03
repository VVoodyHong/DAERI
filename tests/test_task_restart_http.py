"""New-session recovery requires an actual host review and current saved state."""

import asyncio
import time

import anyio
from mcp.types import ElicitResult
import pytest

from daeri.core.cloud import HarnessReply
from daeri.core.access import AuthenticatedUser
from daeri.core.errors import StoreError
from test_agent_service import PLAN, PENDING, setup
from test_mcp_server import connected, factory, serving


@pytest.mark.parametrize('action,content,rotates', [
    ('accept', {'reviewed': True, 'observations': 'The previous run ended; the saved report is valid.'}, True),
    ('accept', {'reviewed': False, 'observations': 'The old run may still be active.'}, False),
    ('accept', {'observations': 'The previous run ended.'}, False),
    ('accept', {'reviewed': True, 'observations': '   '}, False),
    ('accept', {'reviewed': True, 'observations': 'Ended.', 'approved': True}, False),
    ('decline', None, False),
    ('cancel', None, False),
])
def test_http_review_rotates_only_after_explicit_completion_and_observations(action, content, rotates):
    agent, _, store, harness = setup(HarnessReply('The original verified report', 'end_turn'),
                                    HarnessReply('Review the new plan', 'tool_use', (PENDING,)))
    async def check():
        prompts = []
        async def review(context, params):
            prompts.append(params)
            assert '기존 실행의 종료 여부와 실제 변경' in params.message
            assert params.requestedSchema['properties']['reviewed']['default'] is False
            return ElicitResult(action=action, content=content)
        async with serving(factory(agent)) as url:
            async with connected(url, callback=review) as (session, _, __):
                initial = await session.call_tool('daeri_work', {'project_id': 'charging', 'request': 'Compare resources'})
                task_id = initial.structuredContent['task_id']
                original = harness.calls[0][0]
                result = await session.call_tool('daeri_recover', {
                    'project_id': 'charging', 'task_id': task_id, 'restart': True})
                assert not result.isError and len(prompts) == 1
                task = store.get_task(original.context, task_id)
                assert (task.session_id != original.session_id) is rotates
                assert len(harness.calls) == 1 + int(rotates)
                if rotates:
                    assert result.structuredContent['awaiting_confirmation']
                    assert result.structuredContent['plan'] == PLAN
                    assert result.structuredContent['approval'] == 'pending'
                    assert harness.guards[1]['decision'] == 'deny'
                    with pytest.raises(StoreError, match='ACCESS_DENIED'):
                        store.hook_get(original.harness_arn, original.session_id)
                else:
                    assert result.structuredContent['response'] == 'The original verified report'
                    assert not any('/reset/' in key for key in store.client.objects)
    anyio.run(check)


def test_http_recovery_does_not_apply_a_review_when_the_task_changed_in_another_host():
    agent, _, store, harness = setup(HarnessReply('Original report', 'end_turn'),
                                    HarnessReply('A newer report', 'end_turn'))
    task_id = None
    async def check():
        nonlocal task_id
        async def review(context, params):
            await asyncio.to_thread(agent.work, current_user, 'charging', 'A newer request', task_id)
            return ElicitResult(action='accept', content={'reviewed': True, 'observations': 'The previous run ended.'})
        current_user = AuthenticatedUser('alice', frozenset({'charging'}), int(time.time()) + 120)
        async with serving(factory(agent)) as url:
            async with connected(url, callback=review) as (session, _, __):
                started = await session.call_tool('daeri_work', {'project_id': 'charging', 'request': 'Compare resources'})
                task_id = started.structuredContent['task_id']
                original = harness.calls[0][0]
                result = await session.call_tool('daeri_recover', {
                    'project_id': 'charging', 'task_id': task_id, 'restart': True})
                assert result.isError and result.structuredContent['error']['code'] == 'STORAGE_CONFLICT'
                current = store.get_task(original.context, task_id)
                assert current.session_id == original.session_id
                assert len(harness.calls) == 2 and not any('/reset/' in key for key in store.client.objects)
    anyio.run(check)
