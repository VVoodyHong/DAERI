"""Report completion durably saves host-selected knowledge without another host call."""

import copy
from contextlib import closing

import pytest

from daeri.application.runtime import Runtime
from daeri.core.errors import StoreError
from test_embedding_memory import api, api_config
from test_execution_flow import (ROOT, approve, begin, comparison_report, execution,
                                 resource, table_plan)


def completed(runtime, *, memory_none=False, provider="keyword_only"):
    resources = [resource(), resource('cache-b', '7.00')]
    for item in resources:
        item['policy'] = None
    profile = runtime.store.put_profile('service', resources=resources,
                                      embedding=api_config() if provider == 'api' else {'provider': 'keyword_only'})
    for item in resources:
        runtime.registry.diagnose(profile, item['instance_id'])
    proposed = copy.deepcopy(profile)
    for item in proposed['resources']:
        item['policy'] = {'allowed_model_fields': [ROOT], 'export_fields': [ROOT],
            'retained_fields': [ROOT], 'masking': [], 'memory': {
                'categories': ['procedure'], 'source_fields': [ROOT], 'context_sources': [],
                'retention': 'long_term', 'allow_model_recall': True}}
    if memory_none:
        for item in proposed['resources']:
            item['policy']['memory'] = {'categories': [], 'source_fields': [], 'context_sources': [],
                                        'retention': 'none', 'allow_model_recall': False}
    saved, draft = begin(runtime, proposed, configure=table_plan)
    approved = approve(runtime, saved)
    first = runtime.call('daeri_capability_execute', execution(saved, approved, draft['steps'][0]))['data']
    second = runtime.call('daeri_capability_execute', execution(saved,
        {**approved, 'task_revision': first['task_revision']}, draft['steps'][1], call_id='call-2'))['data']
    report = comparison_report(first, second)
    return profile, {'request_id': 'report', 'task_id': saved['task_id'],
        'expected_task_revision': second['task_revision'], 'plan_id': saved['plan_id'],
        'plan_version': saved['plan_version'], 'report': report,
        'memory_candidates': [{'draft': {'category': 'procedure',
            'statement': 'Cache 값을 비교할 때 동일한 기준의 get 조회 결과를 검산한다.',
            'applicability': '이 profile의 캐시 비교', 'instance_ids': ['cache-a', 'cache-b'],
            'verification_method': '실제 완료 조회와 비교 결과 대조'},
            'source_refs': report['checks'][0]['evidence_refs']}]}


@pytest.mark.parametrize('explicit_empty', [False, True])
def test_report_without_host_candidates_skips_memory(tmp_path, explicit_empty):
    with closing(Runtime(tmp_path)) as runtime:
        profile, args = completed(runtime)
        if explicit_empty:
            args['memory_candidates'] = []
        else:
            args.pop('memory_candidates')
        result = runtime.call('daeri_result_submit', args)
        assert result['status'] == 'ok', result
        assert result['data']['memory'] == {'state': 'skipped', 'items': []}
        assert runtime.store._db.execute('SELECT count(*) FROM memory_heads').fetchone()[0] == 0
        assert runtime.store.get_task(args['task_id']).get('memory_job_refs', []) == []
        replay = runtime.call('daeri_result_submit', args)
        assert replay['data']['task_revision'] == result['data']['task_revision']
        assert replay['data']['memory'] == result['data']['memory']


def test_host_candidate_is_processed_with_the_report_and_current_source_checks(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        profile, args = completed(runtime)
        args['memory_candidates'] = [{'draft': {'category': 'procedure',
            'statement': '동일한 비교 기준으로 캐시 값을 조회한 뒤 검산한다',
            'applicability': '이 profile의 캐시 비교', 'instance_ids': ['cache-a', 'cache-b'],
            'verification_method': '실제 완료 조회와 검증된 비교표 대조'},
            'source_refs': args['report']['checks'][0]['evidence_refs']}]
        result = runtime.call('daeri_result_submit', args)
        assert result['status'] == 'ok', result
        assert result['data']['memory']['state'] == 'ready'
        item = runtime.memory.get(profile['profile_id'], result['data']['memory']['items'][0]['memory_ref'])
        assert item['statement'] == args['memory_candidates'][0]['draft']['statement']


def test_failed_automatic_save_retries_after_restart_and_preserves_report_revision(tmp_path, monkeypatch):
    with closing(Runtime(tmp_path)) as runtime:
        profile, args = completed(runtime)
        original = runtime.memory.save
        def unavailable(*args, **kwargs):
            if kwargs.get('validate_only'):
                return original(*args, **kwargs)
            raise StoreError('STORE_UNAVAILABLE', 'temporarily unavailable')
        monkeypatch.setattr(runtime.memory, 'save', unavailable)
        result = runtime.call('daeri_result_submit', args)
        assert result['status'] == 'ok', result
        assert result['data']['memory']['state'] == 'pending'
        revision = result['data']['task_revision']
        assert runtime.store.read_record(args['task_id'], result['data']['report_ref'])['record_kind'] == 'report'
    with closing(Runtime(tmp_path)) as runtime:
        task = runtime.call('daeri_task_create', {'request_id': 'next', 'request_text': 'Cache get',
            'context': {'profile_id': profile['profile_id']}})['data']
        searched = runtime.call('daeri_memory_search', {'task_id': task['task_id'], 'query': 'Cache get'})
        assert searched['status'] == 'ok', searched
        assert len(searched['data']['items']) == 1
        assert runtime.store.get_task(args['task_id'])['task_revision'] == revision
        assert runtime.store._db.execute('SELECT count(*) FROM memory_heads').fetchone()[0] == 1
        assert runtime.call('daeri_result_submit', args)['data']['memory']['state'] == 'ready'


def test_invalid_candidate_does_not_rollback_report_or_store_secret(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        _, args = completed(runtime)
        args['memory_candidates'] = [{'draft': {'category': 'procedure',
            'statement': 'Bearer PRIVATE_EMBEDDING_123456789', 'applicability': 'Cache',
            'instance_ids': ['cache-a'], 'verification_method': 'actual source'},
            'source_refs': args['report']['checks'][0]['evidence_refs']}]
        result = runtime.call('daeri_result_submit', args)
        assert result['status'] == 'ok', result
        assert result['data']['memory']['state'] == 'skipped'
        assert runtime.store._db.execute('SELECT count(*) FROM memory_heads').fetchone()[0] == 0
        for path in tmp_path.joinpath('records').iterdir():
            assert b'Bearer PRIVATE_EMBEDDING_123456789' not in path.read_bytes()


def test_memory_queue_is_internal_and_cannot_be_read_as_a_result(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        _, args = completed(runtime)
        result = runtime.call('daeri_result_submit', args)
        queue_ref = runtime.store.get_task(args['task_id'])['memory_job_refs'][0]
        listed = runtime.call('daeri_task_get', {'task_id': args['task_id']})
        assert queue_ref not in [item['record_ref'] for item in listed['data']['records']]
        hidden = runtime.call('daeri_result_get', {'task_id': args['task_id'], 'record_ref': queue_ref})
        assert hidden['status'] == 'error' and hidden['error']['code'] == 'ACCESS_DENIED'


def test_candidate_validation_outage_retries_the_same_public_report_without_duplicate_memory(tmp_path, monkeypatch):
    with closing(Runtime(tmp_path)) as runtime:
        profile, args = completed(runtime)
        args = {key: value for key, value in args.items()
                if key not in {'request_id', 'expected_task_revision', 'plan_id', 'plan_version'}}
        statement = args['memory_candidates'][0]['draft']['statement']
        original = runtime.memory.save
        def unavailable(*args, **kwargs):
            if kwargs.get('validate_only'):
                raise StoreError('STORE_UNAVAILABLE', 'temporarily unavailable')
            return original(*args, **kwargs)
        monkeypatch.setattr(runtime.memory, 'save', unavailable)
        result = runtime.call('daeri_result_submit', args)
        assert result['status'] == 'ok', result
        assert result['data']['memory'] == {'state': 'skipped', 'items': [
            {'state': 'skipped', 'error_code': 'STORE_UNAVAILABLE', 'next_action': 'resubmit'}]}
        assert runtime.store._db.execute('SELECT count(*) FROM memory_heads').fetchone()[0] == 0
        assert runtime.store._db.execute("SELECT count(*) FROM records WHERE kind='memory_pending'").fetchone()[0] == 0
        for path in tmp_path.joinpath('records').iterdir():
            assert statement.encode() not in path.read_bytes()
        monkeypatch.setattr(runtime.memory, 'save', original)
        recovered = runtime.call('daeri_result_submit', args)
        assert recovered['data']['report_ref'] == result['data']['report_ref']
        assert recovered['data']['memory']['state'] == 'ready', recovered
        memory_ref = recovered['data']['memory']['items'][0]['memory_ref']
        assert runtime.memory.get(profile['profile_id'], memory_ref)['statement'] == statement
        repeated = runtime.call('daeri_result_submit', args)
        assert repeated['data']['memory'] == recovered['data']['memory']
        assert runtime.store._db.execute('SELECT count(*) FROM memory_heads').fetchone()[0] == 1


def test_legacy_pending_job_without_a_candidate_requires_resubmission(tmp_path):
    from daeri.application.automatic_memory import finish_report

    with closing(Runtime(tmp_path)) as runtime:
        _, args = completed(runtime)
        result = runtime.call('daeri_result_submit', args)
        assert result['data']['memory']['state'] == 'ready', result
        task = runtime.store.get_task(args['task_id'])
        legacy = runtime.store.write_record(task['task_id'], 'memory_pending', {
            'report_ref': result['data']['report_ref'],
            'items': [{'state': 'pending', 'error_code': 'STORE_UNAVAILABLE'}]})
        task['memory_job_refs'].append(legacy)
        runtime._save(task)
        summary = finish_report(runtime, result['data'])
        assert summary['items'][-1] == {'state': 'skipped', 'error_code': 'STORE_UNAVAILABLE',
                                        'next_action': 'resubmit'}
        assert runtime.store._db.execute('SELECT count(*) FROM memory_heads').fetchone()[0] == 1


def test_report_replay_reflects_a_later_verified_memory_invalidation(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        _, args = completed(runtime)
        result = runtime.call('daeri_result_submit', args)
        memory_ref = result['data']['memory']['items'][0]['memory_ref']
        invalidated = runtime.call('daeri_memory_update', {'task_id': args['task_id'],
            'request_id': 'invalidate', 'expected_task_revision': result['data']['task_revision'],
            'operation': 'invalidate', 'memory_ref': memory_ref,
            'source_refs': args['report']['checks'][0]['evidence_refs'], 'reason': '사용자가 실제 절차를 정정함'})
        assert invalidated['status'] == 'ok', invalidated
        replay = runtime.call('daeri_result_submit', args)
        assert replay['status'] == 'ok', replay
        assert replay['data']['memory']['state'] == 'skipped'
        assert replay['data']['memory']['items'][0]['error_code'] == 'MEMORY_CONFLICT'


def test_explicit_memory_denial_never_persists_a_host_candidate_body(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        _, args = completed(runtime, memory_none=True)
        private_candidate = '이 후보는 승인한 기억 범위를 벗어난 고유 문장이다'
        args['memory_candidates'] = [{'draft': {'category': 'procedure', 'statement': private_candidate,
            'applicability': 'Cache', 'instance_ids': ['cache-a', 'cache-b'],
            'verification_method': 'actual calls'}, 'source_refs': args['report']['checks'][0]['evidence_refs']}]
        result = runtime.call('daeri_result_submit', args)
        assert result['status'] == 'ok', result
        assert result['data']['memory']['state'] == 'skipped'
        assert runtime.store._db.execute('SELECT count(*) FROM memory_heads').fetchone()[0] == 0
        assert runtime.store._db.execute("SELECT count(*) FROM records WHERE kind='memory_pending'").fetchone()[0] == 0
        for path in tmp_path.joinpath('records').iterdir():
            assert private_candidate.encode() not in path.read_bytes()


def test_search_stops_backlog_retry_at_the_first_api_index_failure(tmp_path, api, monkeypatch):
    def failed_document(self, text):
        self.calls.append(('document', self.model_id, text))
        raise RuntimeError('temporary API failure')
    monkeypatch.setattr(api, 'embed_document', failed_document)
    with closing(Runtime(tmp_path)) as runtime:
        profile, args = completed(runtime, provider='api')
        first = runtime.call('daeri_result_submit', args)
        second = runtime.call('daeri_result_submit', {**args, 'request_id': 'report-again',
            'expected_task_revision': first['data']['task_revision']})
        assert first['data']['memory']['state'] == second['data']['memory']['state'] == 'pending'
        api.calls.clear()
        next_task = runtime.call('daeri_task_create', {'request_id': 'next', 'request_text': 'Cache get',
            'context': {'profile_id': profile['profile_id']}})['data']
        searched = runtime.call('daeri_memory_search', {'task_id': next_task['task_id'], 'query': 'Cache get'})
        assert searched['status'] == 'partial' and searched['data']['items']
        assert [kind for kind, *_ in api.calls].count('document') == 1
        assert runtime.store._db.execute("SELECT count(*) FROM records WHERE kind='memory_pending'").fetchone()[0] == 2
