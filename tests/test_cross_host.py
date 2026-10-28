"""Two installation-local MCP processes share profile, work, files and memory."""

import asyncio
from contextlib import closing
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from daeri.infrastructure.storage import Store
from daeri.application.runtime import Runtime
from test_automatic_memory import completed
from test_embedding_integration import company_api
from test_first_work_defaults import connection_only_profile
from test_mcp_server import connect, envelope
from test_simple_workflow import simple_plan


@pytest.mark.parametrize('first_host', ['claude', 'codex'])
def test_hosts_continue_approved_work_and_recall_each_others_memory(
        tmp_path, plugin_environment, first_host):
    data = tmp_path / 'data'
    with closing(Store(data)) as store:
        profile = connection_only_profile(store, 'keyword_only')
    other_host = 'codex' if first_host == 'claude' else 'claude'
    plugins = {first_host: plugin_environment, other_host: tmp_path / 'other plugin'}
    shutil.copytree(plugin_environment, plugins[other_host], symlinks=True)
    for plugin in plugins.values():
        subprocess.run([str(plugin / '.venv/bin/python.exe'), '-I',
                        str(plugin / 'scripts/register_mcp.py')],
                       env=dict(os.environ, DAERI_DATA_DIR=str(data)), check=True, timeout=10)

    async def scenario():
        async with connect(data, plugin_root=plugins[first_host]) as (first, _):
            async with connect(data, plugin_root=plugins[other_host]) as (second, _):
                async def call(session, tool, **args):
                    result = envelope(await session.call_tool(tool, args))
                    assert result['status'] == 'ok', result
                    return result['data']

                task = await call(first, 'daeri_task_create', request_text='Cache 비교를 이어서 수행한다',
                                  context={'profile_id': profile['profile_id']})
                recalled = await call(first, 'daeri_memory_search', task_id=task['task_id'], query='Cache 비교')
                draft = simple_plan(profile)
                draft['memory_search_refs'] = [recalled['memory_search_ref']]
                saved = await call(first, 'daeri_plan_submit', task_id=task['task_id'], draft=draft)
                approved = await call(first, 'daeri_approval_request', task_id=task['task_id'],
                                      plan_id=saved['plan_id'], decision='approve', user_response='진행해')
                original = await call(first, 'daeri_capability_execute', task_id=task['task_id'],
                                      step_id='read-0', parameters={'key': 'orders'})
                workspace = Path(task['artifact_workspace'])
                output = workspace / 'comparison.csv'
                output.write_text('unfinished host analysis', encoding='utf-8')

                listing = await call(second, 'daeri_task_get', profile_id=profile['profile_id'])
                assert task['task_id'] in {item['task_id'] for item in listing['tasks']}
                observed = await call(second, 'daeri_task_get', task_id=task['task_id'])
                assert observed['task']['artifact_workspace'] == str(workspace)
                request = await call(second, 'daeri_result_get', task_id=task['task_id'],
                                     record_ref=observed['task']['request_ref'])
                assert request['payload']['request_text'] == 'Cache 비교를 이어서 수행한다'
                assert original['result_ref'] in {item['record_ref'] for item in observed['records']}
                plan = await call(second, 'daeri_plan_get', task_id=task['task_id'], plan_id=saved['plan_id'])
                assert plan['plan']['approval_request']['approval_id'] == approved['approval_id']

                resumed = await call(second, 'daeri_task_resume', task_id=task['task_id'], handoff=True)
                assert resumed['resumed'] is True
                stale = envelope(await first.call_tool('daeri_capability_execute', {
                    'task_id': task['task_id'], 'step_id': 'read-1', 'parameters': {'key': 'orders'}}))
                assert stale['error']['code'] == 'OWNER_CONFLICT'
                following = await call(second, 'daeri_capability_execute', task_id=task['task_id'],
                                       step_id='read-1', parameters={'key': 'orders'})
                sources = [original['result_ref'], following['result_ref']]
                output.write_text('ID,A,B,Difference\none,10,7,3\n', encoding='utf-8')
                report = await call(second, 'daeri_result_submit', task_id=task['task_id'],
                    report='두 호스트에서 확보한 Cache 비교 결과의 차이는 3입니다.',
                    evidence_refs=sources,
                    artifacts=[{'artifact_id': 'comparison', 'label': '비교 결과',
                        'local_path': str(output), 'media_type': 'text/csv', 'source_refs': sources}],
                    memory_candidates=[{'draft': {'category': 'procedure',
                        'statement': 'Cache 비교는 같은 식별자의 값을 같은 기준으로 대조하고 차이를 검산한다.',
                        'applicability': '현재 profile의 캐시 비교',
                        'instance_ids': ['cache-a', 'cache-b'],
                        'verification_method': '두 호스트의 실제 조회와 최종 비교 파일 대조'},
                        'source_refs': sources}])
                assert report['memory']['state'] == 'ready', report['memory']
                memory_ref = report['memory']['items'][0]['memory_ref']
                restored = await call(first, 'daeri_result_get', task_id=task['task_id'],
                                      record_ref=report['report_ref'])
                assert set(restored['payload']['evidence_refs']) == set(sources)
                artifact = await call(first, 'daeri_result_get', task_id=task['task_id'],
                                     record_ref=report['artifact_refs'][0])
                assert Path(artifact['managed_file']['local_path']).read_text() == 'ID,A,B,Difference\none,10,7,3\n'
                for session in (first, second):
                    new = await call(session, 'daeri_task_create', request_text='Cache 비교',
                                     context={'profile_id': profile['profile_id']})
                    memory = await call(session, 'daeri_memory_search', task_id=new['task_id'], query='Cache 비교')
                    assert memory_ref in {item['memory_ref'] for item in memory['items']}
                    assert memory['retrieval_state'] == 'keyword_only'
    asyncio.run(scenario())


def test_another_runtime_reuses_api_vectors_without_recreating_profile_or_index(tmp_path, company_api):
    with closing(Runtime(tmp_path)) as previous, closing(Runtime(tmp_path)) as current:
        profile, args = completed(previous, provider='api')
        handed = current.call('daeri_task_resume', {'task_id': args['task_id'], 'handoff': True})
        assert handed['data']['resumed'] is True
        args.pop('expected_task_revision')
        report = current.call('daeri_result_submit', args)
        assert report['status'] == 'ok', report
        assert report['data']['memory']['state'] == 'ready'
        reference = report['data']['memory']['items'][0]['memory_ref']
        before = tuple(current.store._db.execute('SELECT * FROM memory_vectors').fetchone())
        for runtime in (previous, current):
            task = runtime.call('daeri_task_create', {'request_text': 'Cache 비교',
                'context': {'profile_id': profile['profile_id']}})['data']
            recalled = runtime.call('daeri_memory_search', {'task_id': task['task_id'],
                'query': 'semantic-question-without-keyword-match'})
            assert recalled['data']['retrieval_state'] == 'hybrid', recalled
            assert reference in {item['memory_ref'] for item in recalled['data']['items']}
            assert runtime.store.get_profile(profile['profile_id']) == profile
        assert tuple(current.store._db.execute('SELECT * FROM memory_vectors').fetchone()) == before
        assert len(company_api['requests']) == 3
