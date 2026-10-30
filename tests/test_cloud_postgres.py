"""Aurora boundary contracts; SQL execution against AWS is a separate integration check."""

import json
from uuid import UUID

import pytest

from daeri.core.cloud import AccessContext, CloudTask, HarnessReply, KnowledgeJob
from daeri.core.errors import StoreError
from daeri.infrastructure.cloud_postgres import AuroraCloudStore


class DataApi:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def execute_statement(self, **request):
        self.calls.append(('execute', request))
        result = self.results.pop(0) if self.results else []
        if isinstance(result, Exception):
            raise result
        return {'records': [[{'stringValue': json.dumps(item)}] for item in result]}

    def begin_transaction(self, **request):
        self.calls.append(('begin', request))
        return {'transactionId': 'transaction'}

    def commit_transaction(self, **request):
        self.calls.append(('commit', request))

    def rollback_transaction(self, **request):
        self.calls.append(('rollback', request))


CONTEXT = AccessContext('alice', 'ev', 'charging', frozenset({'db', 'docs'}))
TASK = CloudTask('task-a', '01234567-89ab-cdef-0123-456789abcdef', CONTEXT)


def store(client):
    return AuroraCloudStore(client, 'arn:aws:rds:us-east-1:123456789012:cluster:test',
                            'arn:aws:secretsmanager:us-east-1:123456789012:secret:test', 'daeri')


def parameters(request):
    return {item['name']: next(iter(item['value'].values())) for item in request['parameters']}


def test_task_lookup_binds_current_identity_and_all_scope_dimensions():
    client = DataApi([])
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        store(client).get_task(CONTEXT, 'other-task')
    request = client.calls[0][1]
    assert parameters(request) == {'id': 'other-task', 'actor': 'alice', 'project': 'ev',
                                   'service': 'charging', 'sources': '["db","docs"]', 'part_offset': 1}
    for dimension in ('actor_id', 'project_id', 'service_id', 'source_ids'):
        assert dimension in request['sql']


def test_result_and_knowledge_registration_commit_together():
    client = DataApi([{'id': 'task-a'}], [], [])
    store(client).finish_turn(TASK, 'turn-a', 'count stations', HarnessReply('45 countries', 'end_turn'), ())
    operations = [name for name, _ in client.calls]
    assert operations == ['begin', 'execute', 'execute', 'execute', 'commit']
    requests = [request for name, request in client.calls if name == 'execute']
    assert all(request['transactionId'] == 'transaction' for request in requests)
    assert 'cloud_knowledge_jobs' in requests[-1]['sql']


def test_storage_failure_rolls_back_result_and_outbox_without_raw_error_details():
    client = DataApi([{'id': 'task-a'}], RuntimeError('password=private-db-password'))
    with pytest.raises(StoreError) as error:
        store(client).finish_turn(TASK, 'turn-a', 'count', HarnessReply('result', 'end_turn'), ())
    assert error.value.code == 'STORAGE_UNAVAILABLE'
    assert 'private-db-password' not in str(error.value)
    assert client.calls[-1][0] == 'rollback'
    assert not any(name == 'commit' for name, _ in client.calls)


@pytest.mark.parametrize('reply', [HarnessReply('partial', 'interrupted', error='HARNESS_UNAVAILABLE'),
                                       HarnessReply('confirm?', 'tool_use', ({'toolUseId': 'approve'},)),
                                       HarnessReply('', 'end_turn')])
def test_incomplete_invocations_are_recorded_without_publishing_successful_experience(reply):
    client = DataApi([{'id': 'task-a'}], [])
    store(client).finish_turn(TASK, 'turn-a', 'count', reply, ())
    assert [name for name, _ in client.calls] == ['begin', 'execute', 'execute', 'commit']


def test_busy_task_never_acquires_a_second_turn():
    client = DataApi([])
    with pytest.raises(StoreError, match='TASK_BUSY'):
        store(client).begin_turn(TASK)


def test_hybrid_query_filters_scope_and_embedding_space_before_ranking_and_binds_user_text():
    query = "country'); DROP TABLE cloud_tasks; --"
    client = DataApi([{'id': 'memory-a'}], [{'part': json.dumps({
        'id': 'memory-a', 'text': 'country_code is country', 'source_ids': ['db']})}])
    result = store(client).search(CONTEXT, query, [0.5, 0.25], 'model-a:2', 5)
    request = client.calls[0][1]
    args = parameters(request)
    assert query not in request['sql']
    assert args['query'] == query
    assert args['project'] == 'ev' and args['service'] == 'charging'
    assert args['sources'] == '["db","docs"]'
    assert args['model'] == 'model-a:2' and args['dimensions'] == 2
    assert args['embedding'] == '[0.5,0.25]'
    assert result[0].source_ids == frozenset({'db'})
    assert 'source_ids <@' in request['sql']
    assert 'vector_dims(embedding)' in request['sql']


def test_keyword_fallback_omits_vector_distance_operation():
    client = DataApi([])
    assert store(client).search(CONTEXT, 'country', None, None, 5) == []
    assert '<=>' not in client.calls[0][1]['sql']


def test_expired_job_lease_cannot_replace_another_workers_memory():
    client = DataApi([])
    job = KnowledgeJob('turn-a', 'old-token', CONTEXT, 'count', '45 countries')
    with pytest.raises(StoreError, match='JOB_LEASE_EXPIRED'):
        store(client).save_text(job, 'validated experience')
    assert parameters(client.calls[0][1])['token'] == 'old-token'


def test_knowledge_retry_retains_plaintext_and_releases_lease():
    client = DataApi([{'id': 'turn-a'}])
    job = KnowledgeJob('turn-a', 'lease-a', CONTEXT, 'count', '45 countries', 'country_code is country')
    store(client).retry_job(job, 'EMBEDDING_FAILED')
    assert all('DELETE' not in request['sql'] for name, request in client.calls if name == 'execute')
    assert parameters(client.calls[0][1])['code'] == 'EMBEDDING_FAILED'


def test_new_task_uses_distinct_server_generated_task_and_harness_session_ids():
    client = DataApi()
    task = store(client).create_task(CONTEXT)
    assert UUID(task.id) and UUID(task.session_id)
    assert task.id != task.session_id


def test_data_api_parameters_match_installed_aws_sdk_contract():
    botocore = pytest.importorskip('botocore.session')
    from botocore.validate import validate_parameters
    model = botocore.get_session().get_service_model('rds-data')
    client = DataApi([])
    store(client).search(CONTEXT, 'country', [0.5, 0.25], 'model-a:2', 5)
    validate_parameters(client.calls[0][1], model.operation_model('ExecuteStatement').input_shape)


class BoundedDataApi(DataApi):
    """The external row limit is enforced on native encoded SDK responses."""

    def __init__(self, document, claim=False):
        super().__init__()
        self.document = json.dumps(document, ensure_ascii=False)
        self.claim = claim

    def execute_statement(self, **request):
        self.calls.append(('execute', request))
        if 'part_offset' in parameters(request):
            offset = parameters(request)['part_offset'] - 1
            payload = {'part': self.document[offset:offset + 8192]}
        elif self.claim:
            payload = {'id': 'turn-a', 'token': 'lease-a'}
        else:
            payload = json.loads(self.document)
        encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if len(encoded) >= 65536:
            raise RuntimeError('Packet for query is too large')
        return {'records': [[{'stringValue': encoded.decode('utf-8')}]]}


def test_large_pending_tool_input_and_goal_survive_data_api_row_limit():
    pending = {'toolUseId': 'approve', 'name': 'confirm_plan', 'input': {'plan': '\\"한🙂' * 15000}}
    document = {'id': 'task-a', 'session_id': TASK.session_id, 'pending_tools': [pending],
                'goal': '충전소 집계' * 16000}
    client = BoundedDataApi(document)
    task = store(client).get_task(CONTEXT, 'task-a')
    assert task.pending_tools == (pending,)
    assert task.goal == document['goal']
    assert len(client.calls) > 1


def test_large_prompt_result_and_summary_are_claimed_without_truncating_evidence():
    document = {'id': 'turn-a', 'token': 'lease-a', 'actor': 'alice', 'project': 'ev',
                'service': 'charging', 'sources': ['db'], 'prompt': 'p' * 40960,
                'response': 'r' * 40960, 'text': '경험' * 12000}
    client = BoundedDataApi(document, claim=True)
    job = store(client).claim_job()
    assert job.prompt == document['prompt']
    assert job.response == document['response']
    assert job.text == document['text']
    assert len(client.calls) > 3
