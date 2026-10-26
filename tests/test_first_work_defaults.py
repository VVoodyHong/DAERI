"""Connection-only setup reaches approved reporting and selective reusable memory."""

import copy
from contextlib import closing
from pathlib import Path

import pytest

from daeri.application.runtime import Runtime
from daeri.config.onboarding import create_draft, validate_draft, apply_draft
from daeri.infrastructure.resources import ResourceRegistry
from daeri.config.setup_yaml import read_yaml, write_yaml
from daeri.infrastructure.storage import Store
from test_embedding_memory import api, api_config
from test_execution_flow import comparison_report, resource
from test_simple_workflow import simple_plan
from test_memory import NumericalFixtureEmbedder


def connection_only_profile(store, provider):
    draft = create_draft(store, 'service', {'cache-a': 'cache-redis', 'cache-b': 'cache-redis'},
                         embedding='keyword_only')
    path = Path(draft['path'])
    document = read_yaml(path)
    for block in document['resources'].values():
        assert 'policy' not in block
        block['inputs'].update(host='example.invalid', port=6379, db=0, tls=True)
    assert 'memory_policy' not in document
    if provider == 'api':
        document['embedding'] = api_config()
    elif provider == 'local':
        document['embedding'] = {'provider': 'local'}
    path.write_text(write_yaml(document), encoding='utf-8')
    checked = validate_draft(store, 'service')
    assert checked['valid'], checked
    configured = apply_draft(store, 'service', checked['confirmation_token'])
    # Model resource preparation with actual local fixture MCPs instead of company services.
    for index, item in enumerate(configured['resources']):
        fixture = resource(item['instance_id'], '10.00' if index == 0 else '7.00')
        item['connection'].update(command=fixture['connection']['command'],
                                  args=fixture['connection']['args'], auth_env={},
                                  env={**item['connection']['env'], **fixture['connection']['env']})
        item['capabilities'][0]['capability_id'] = 'get'
    configured = store.put_profile('service', resources=configured['resources'],
                                   embedding=configured['embedding'])
    for item in configured['resources']:
        assert item.get('policy') is None
        ResourceRegistry(store).diagnose(configured, item['instance_id'])
    assert configured['memory_policy'] is None
    return configured


@pytest.mark.parametrize('provider', ['keyword_only', 'api', 'local'])
@pytest.mark.parametrize('legacy', [False, True])
def test_setup_to_approved_report_file_memory_and_next_task_without_policy_edits(tmp_path, api, provider, legacy):
    with closing(Store(tmp_path)) as store:
        profile = connection_only_profile(store, provider)
        if legacy:
            previous = copy.deepcopy(profile)
            previous['memory_policy'] = {}
            for item in previous['resources']:
                item['policy'] = {}
            if provider == 'api':
                previous['embedding']['allow_remote'] = False
            path, size, digest = store._prepare('profiles', previous)
            store._db.execute('UPDATE profiles SET path=?,size=?,sha256=? WHERE profile_id=?',
                (path, size, digest, previous['profile_id']))
            for item in previous['resources']:
                ResourceRegistry(store).diagnose(previous, item['instance_id'])
            profile = store.get_profile(previous['profile_id'])
    with closing(Runtime(tmp_path)) as runtime:
        if provider == 'local':
            runtime.memory.embedder = NumericalFixtureEmbedder()
        task = runtime.call('daeri_task_create', {'request_text': 'Cache 비교',
            'context': {'profile_id': profile['profile_id']}})['data']
        recalled = runtime.call('daeri_memory_search', {'task_id': task['task_id'], 'query': 'Cache 비교'})
        assert recalled['status'] == 'ok', recalled
        draft = simple_plan(profile)
        draft['memory_search_refs'] = [recalled['data']['memory_search_ref']]
        saved = runtime.call('daeri_plan_submit', {'task_id': task['task_id'], 'draft': draft})['data']
        blocked = runtime.call('daeri_capability_execute', {'task_id': task['task_id'],
            'step_id': 'read-0', 'parameters': {'key': 'orders'}})
        assert blocked['status'] == 'error'
        assert runtime.store.get_task(saved['task_id'])['calls'] == []
        approved = runtime.call('daeri_approval_request', {'task_id': task['task_id'],
            'plan_id': saved['plan_id'], 'decision': 'approve', 'user_response': '진행해'})
        assert approved['status'] == 'ok', approved
        first = runtime.call('daeri_capability_execute', {'task_id': task['task_id'],
            'step_id': 'read-0', 'parameters': {'key': 'orders'}})
        assert first['status'] == 'ok', first
        second = runtime.call('daeri_capability_execute', {'task_id': task['task_id'],
            'step_id': 'read-1', 'parameters': {'key': 'orders'}})
        assert second['status'] == 'ok', second
        body = comparison_report(first['data'], second['data'])
        sources = [first['data']['result_ref'], second['data']['result_ref']]
        output = Path(runtime.store.artifact_workspace(saved['task_id'])) / 'comparison.csv'
        output.write_text('ID,A,B,Difference\none,10,7,3\n', encoding='utf-8')
        body['artifacts'] = [{'artifact_id': 'comparison-csv', 'label': '캐시 비교',
            'local_path': str(output), 'media_type': 'text/csv', 'source_refs': sources}]
        report = runtime.call('daeri_result_submit', {'task_id': saved['task_id'], 'report': body,
            'memory_candidates': [{'draft': {'category': 'procedure',
                'statement': 'Cache 비교는 같은 식별자의 조회를 같은 기준으로 대조하고 차이를 검산한다.',
                'applicability': '현재 profile의 캐시 비교', 'instance_ids': ['cache-a', 'cache-b'],
                'verification_method': '실제 승인한 조회 결과와 호스트가 만든 비교 파일 대조'},
                'source_refs': sources}]})
        assert report['status'] == 'ok', report
        assert report['data']['memory']['state'] == 'ready', report
        memories = [item['memory_ref'] for item in report['data']['memory']['items']
                    if item['state'] == 'ready']
        assert memories
        artifact = runtime.call('daeri_result_get', {'task_id': saved['task_id'],
            'record_ref': report['data']['artifact_refs'][0]})
        assert artifact['status'] == 'ok', artifact
        assert Path(artifact['data']['managed_file']['local_path']).read_text() == 'ID,A,B,Difference\none,10,7,3\n'
        for reference in memories:
            statement = runtime.memory.get(profile['profile_id'], reference)['statement']
            assert '10.00' not in statement and '7.00' not in statement and 'orders' not in statement
        next_task = runtime.call('daeri_task_create', {'request_text': 'Cache 비교',
            'context': {'profile_id': profile['profile_id']}})['data']
        recalled = runtime.call('daeri_memory_search', {'task_id': next_task['task_id'], 'query': 'Cache 비교'})
        assert recalled['status'] == 'ok', recalled
        assert set(memories) <= {item['memory_ref'] for item in recalled['data']['items']}
        assert recalled['data']['retrieval_state'] == ('keyword_only' if provider == 'keyword_only' else 'hybrid')
        assert runtime.store.get_task(next_task['task_id'])['calls'] == []
        assert runtime.store.get_profile('service')['memory_policy'] is None
        if provider == 'api':
            assert {call[0] for call in api.calls} == {'document', 'query'}
        else:
            assert api.calls == []
        vectors = runtime.store._db.execute('SELECT count(*) FROM memory_vectors').fetchone()[0]
        assert vectors == (0 if provider == 'keyword_only' else len(memories))
