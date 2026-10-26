"""An actual user correction replaces past experience for future planning."""

from contextlib import closing

from daeri.application.runtime import Runtime
from test_automatic_memory import completed
from test_execution_flow import ROOT, approve


def test_user_feedback_is_remembered_and_invalidated_experience_is_not_recalled(tmp_path):
    with closing(Runtime(tmp_path)) as runtime:
        profile, report_args = completed(runtime)
        original = runtime.call('daeri_result_submit', report_args)
        assert original['status'] == 'ok', original
        old_ref = original['data']['memory']['items'][0]['memory_ref']
        feedback = '캐시 값을 비교할 때는 UTC를 기준으로 같은 시점의 값을 비교한다.'
        task = runtime.call('daeri_task_create', {'request_id': 'feedback', 'request_text': feedback,
            'context': {'profile_id': profile['profile_id']}})['data']
        request_ref = runtime.store.get_task(task['task_id'])['request_ref']
        invalidated = runtime.call('daeri_memory_update', {'request_id': 'correct-old',
            'task_id': task['task_id'], 'expected_task_revision': task['task_revision'],
            'operation': 'invalidate', 'memory_ref': old_ref, 'source_refs': [request_ref],
            'reason': '사용자가 비교 기준을 정정했다'})
        assert invalidated['status'] == 'ok', invalidated
        search = runtime.call('daeri_memory_search', {'task_id': task['task_id'], 'query': 'Cache 비교'})
        assert old_ref not in {item['memory_ref'] for item in search['data']['items']}
        draft = {'purpose': '사용자가 정정한 비교 절차를 반영한다', 'steps': [],
            'budget': {'max_tool_calls': 0, 'max_external_requests': 0, 'max_runtime_ms': 0, 'resource_limits': []},
            'output': {'format': 'text', 'destination': '현재 대화', 'resource_rules': [
                {'instance_id': item['instance_id'], 'allowed_model_fields': [ROOT],
                 'export_fields': [ROOT], 'masking': []} for item in profile['resources']],
                },
            'memory_search_refs': [search['data']['memory_search_ref']], 'memory_refs': [], 'memory_limitations': [],
            'completion_criteria': [{'criterion_id': 'feedback', 'description': '실제 사용자 정정을 반영한다',
                                     'required': True, 'step_ids': []}]}
        saved = runtime.call('daeri_plan_submit', {'request_id': 'feedback-plan', 'task_id': task['task_id'],
            'expected_task_revision': invalidated['data']['task_revision'], 'draft': draft})
        assert saved['status'] == 'ok', saved
        approved = approve(runtime, saved['data'])
        result = runtime.call('daeri_result_submit', {'request_id': 'feedback-report', 'task_id': task['task_id'],
            'expected_task_revision': approved['task_revision'], 'plan_id': saved['data']['plan_id'],
            'plan_version': saved['data']['plan_version'],
            'report': {'summary': '사용자가 확인한 비교 기준을 반영했다', 'proposed_task_state': 'succeeded',
                'findings': [{'kind': 'observation', 'text': feedback, 'evidence_refs': [request_ref]}],
                'tables': [], 'artifacts': [], 'checks': [{'criterion_id': 'feedback', 'status': 'met',
                    'method': '실제 사용자 메시지 대조', 'evidence_refs': [request_ref]}],
                'limitations': [], 'uncertain_effects': [], 'next_actions': []},
            'memory_candidates': [{'draft': {'category': 'procedure', 'statement': feedback,
                'applicability': '이 profile의 캐시 비교', 'instance_ids': ['cache-a', 'cache-b'],
                'verification_method': '사용자가 명시한 정정 메시지'}, 'source_refs': [request_ref]}]})
        assert result['status'] == 'ok', result
        assert result['data']['memory']['state'] == 'ready', result
        new_ref = result['data']['memory']['items'][0]['memory_ref']
        following = runtime.call('daeri_task_create', {'request_id': 'after-feedback', 'request_text': '캐시 비교',
            'context': {'profile_id': profile['profile_id']}})['data']
        recalled = runtime.call('daeri_memory_search', {'task_id': following['task_id'], 'query': '캐시'})
        refs = {item['memory_ref'] for item in recalled['data']['items']}
        assert new_ref in refs and old_ref not in refs
        assert runtime.store.get_task(following['task_id'])['calls'] == []
