"""Real cloud use cases with in-memory persistence and external model boundaries."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from daeri.application.cloud import CloudWorkflow, SharedKnowledgeProcessor
from daeri.core.cloud import AccessContext, CloudTask, HarnessReply, KnowledgeJob, SharedMemory
from daeri.core.errors import StoreError


CONTEXT = AccessContext('alice', 'project', 'cache', frozenset({'repository'}))
PENDING = {'toolUseId': 'approval-1', 'name': 'confirm_plan',
           'input': {'plan': 'Inspect cache'}, 'type': 'tool_use'}
RESULT = {'toolUseId': 'approval-1', 'status': 'success',
          'content': [{'json': {'decision': 'approve', 'response': 'Please proceed'}}]}


class MemoryStore:
    """Persistence substitute; production application and its call ordering stay real."""

    def __init__(self):
        self.tasks = {}
        self.turns = []
        self.jobs = {}
        self.memories = {}
        self.searches = []
        self.busy = set()
        self.events = []
        self.fail_capture = False

    def create_task(self, context):
        key = str(len(self.tasks) + 1)
        task = CloudTask(key, f'session-{key}', context)
        self.tasks[key] = task
        return task

    def get_task(self, context, task_id):
        task = self.tasks[task_id]
        if task.context != context:
            raise StoreError('ACCESS_DENIED', 'Task access denied')
        return task

    def begin_turn(self, task):
        if task.id in self.busy:
            raise StoreError('TASK_BUSY', 'A turn is already running')
        self.busy.add(task.id)
        self.events.append('begin')
        return f'turn-{len(self.turns) + 1}'

    def finish_turn(self, task, turn_id, prompt, reply, recalled_ids):
        self.events.append('finish')
        if self.fail_capture:
            raise StoreError('CAPTURE_FAILED', 'Could not persist the external result')
        self.turns.append(deepcopy((task, turn_id, prompt, reply, recalled_ids)))
        self.tasks[task.id] = replace(task, pending_tools=reply.pending_tools, goal=task.goal or prompt)
        self.busy.remove(task.id)
        if reply.stop_reason == 'end_turn' and reply.text and not reply.error and not reply.pending_tools:
            self.jobs[turn_id] = KnowledgeJob(turn_id, f'token-{turn_id}', task.context, prompt, reply.text)

    def search(self, context, query, vector, model_id, limit):
        self.searches.append((context, query, vector, model_id, limit))
        return [memory for memory, origin in self.memories.values()
                if origin.project_id == context.project_id and origin.service_id == context.service_id
                and memory.source_ids <= context.source_ids][:limit]

    def claim_job(self):
        return next(iter(self.jobs.values()), None)

    def save_text(self, job, text):
        self.events.append('save_text')
        self.jobs[job.id] = replace(job, text=text)
        if text:
            self.memories[job.id] = (SharedMemory(job.id, text, job.context.source_ids), job.context)

    def complete_job(self, job, vector, model_id):
        self.events.append(('complete', vector, model_id))
        del self.jobs[job.id]

    def retry_job(self, job, code):
        self.events.append(('retry', code))


class HarnessBoundary:
    def __init__(self, store, *replies):
        self.store = store
        self.replies = list(replies)
        self.calls = []

    def invoke(self, task, messages):
        assert task.id in self.store.busy, 'External invocation must follow durable begin_turn'
        self.store.events.append('invoke')
        self.calls.append((task, deepcopy(messages)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class ExtractorBoundary:
    def __init__(self, result='Compare cache values using the same identifier.'):
        self.result = result
        self.calls = []

    def extract(self, prompt, response):
        self.calls.append((prompt, response))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class EmbedderBoundary:
    model_id = 'fixture:model-v1'

    def __init__(self, store=None, fail_query=False, fail_document=False):
        self.store = store
        self.fail_query = fail_query
        self.fail_document = fail_document
        self.queries = []

    def embed_query(self, text):
        self.queries.append(text)
        if self.fail_query:
            raise RuntimeError('private-query-token')
        return [0.25, 0.75]

    def embed_document(self, text):
        if self.store:
            assert self.store.memories, 'Text must be searchable before embedding starts'
        if self.fail_document:
            raise RuntimeError('private-embedding-token')
        return [0.5, 0.5]


def memory_context(messages):
    blocks = [block for message in messages for block in message['content'] if 'text' in block]
    return json.loads(blocks[-1]['text'])


def test_another_user_recalls_experience_captured_by_the_real_workflow():
    store = MemoryStore()
    harness = HarnessBoundary(store, HarnessReply('The cache comparison is verified.', 'end_turn'),
                              HarnessReply('I will use the earlier comparison procedure.', 'end_turn'))
    first = CloudWorkflow(store, harness).start(CONTEXT, 'Compare the cache')
    extraction = ExtractorBoundary()
    assert SharedKnowledgeProcessor(store, extraction).process_next() is True
    current = replace(CONTEXT, actor_id='bob')
    second = CloudWorkflow(store, harness).start(current, 'Compare cache again')

    assert first.task_id != second.task_id
    assert second.recalled_ids == (first.turn_id,)
    assert extraction.calls == [('Compare the cache', 'The cache comparison is verified.')]
    recalled = memory_context(harness.calls[-1][1])
    assert recalled['memories'] == [{'id': first.turn_id,
        'text': 'Compare cache values using the same identifier.', 'source_ids': ['repository']}]
    assert 'untrusted' in recalled['instruction'].lower()
    assert store.searches[-1][:2] == (current, 'Compare cache again')
    assert store.turns[-1][-1] == (first.turn_id,)


@pytest.mark.parametrize('changed', [
    {'project_id': 'other'}, {'service_id': 'other'}, {'source_ids': frozenset()},
])
def test_recall_uses_the_current_project_service_and_sources(changed):
    store = MemoryStore()
    store.memories['private'] = (SharedMemory('private', 'Private cache procedure', CONTEXT.source_ids), CONTEXT)
    harness = HarnessBoundary(store, HarnessReply('Plan', 'end_turn'))
    turn = CloudWorkflow(store, harness).start(replace(CONTEXT, **changed), 'cache')
    assert turn.recalled_ids == ()
    assert memory_context(harness.calls[0][1])['memories'] == []


def test_pending_tool_results_preserve_actual_user_response_and_native_sdk_shape():
    store = MemoryStore()
    observed = ({'toolUseId': 'read-1', 'status': 'success', 'content': [{'text': 'Verified'}]},)
    harness = HarnessBoundary(store, HarnessReply('Approve this plan', 'tool_use', (PENDING,)),
                              HarnessReply('Done', 'end_turn', observations=observed))
    app = CloudWorkflow(store, harness)
    started = app.start(CONTEXT, 'Inspect cache')
    supplied = deepcopy(RESULT)
    resumed = app.resume(CONTEXT, started.task_id, 'Use the current source', (supplied,))

    assert harness.calls[1][0].pending_tools == (PENDING,)
    blocks = harness.calls[1][1][0]['content']
    assert {'toolResult': {**RESULT, 'type': 'tool_use'}} in blocks
    assert {'text': 'Use the current source'} in blocks
    assert 'type' not in supplied
    assert resumed.reply.observations == observed
    assert store.turns[-1][3].observations == observed
    assert len(store.searches) == 2


def test_reversed_results_are_sent_in_the_original_pending_call_order():
    store = MemoryStore()
    second_pending = {**PENDING, 'toolUseId': 'approval-2'}
    second_result = {'toolUseId': 'approval-2', 'status': 'error',
                     'content': [{'text': 'The user declined this part'}]}
    harness = HarnessBoundary(store, HarnessReply('Approve both', 'tool_use', (PENDING, second_pending)),
                              HarnessReply('Continued with the approved part', 'end_turn'))
    app = CloudWorkflow(store, harness)
    started = app.start(CONTEXT, 'Inspect cache')
    app.resume(CONTEXT, started.task_id, tool_results=(second_result, RESULT))

    results = [block['toolResult'] for block in harness.calls[1][1][0]['content'] if 'toolResult' in block]
    assert results == [{**RESULT, 'type': 'tool_use'}, {**second_result, 'type': 'tool_use'}]


@pytest.mark.parametrize('resume_prompt', ['', ' \n'])
def test_approval_only_resume_recalls_using_the_original_goal(resume_prompt):
    class KeywordStore(MemoryStore):
        def search(self, context, query, vector, model_id, limit):
            matches = super().search(context, query, vector, model_id, limit)
            return matches if query.strip() else []

    store = KeywordStore()
    harness = HarnessBoundary(store, HarnessReply('Approve', 'tool_use', (PENDING,)),
                              HarnessReply('Continued', 'end_turn'))
    embedder = EmbedderBoundary()
    app = CloudWorkflow(store, harness, embedder)
    started = app.start(CONTEXT, 'Inspect cache')
    store.memories['experience'] = (SharedMemory('experience', 'Cache procedure', CONTEXT.source_ids), CONTEXT)
    resumed = app.resume(CONTEXT, started.task_id, resume_prompt, (RESULT,))

    assert resumed.recalled_ids == ('experience',)
    assert store.searches[-1][1] == 'Inspect cache'
    assert embedder.queries == ['Inspect cache', 'Inspect cache']
    if not resume_prompt:
        assert {'text': 'Inspect cache'} not in harness.calls[-1][1][0]['content']


@pytest.mark.parametrize('results', [
    (), ({**RESULT, 'toolUseId': 'unrelated'},), (RESULT, RESULT),
    ({'toolUseId': 'approval-1', 'content': []},),
    ({**RESULT, 'status': 'approved'},), ({**RESULT, 'content': 'I approve'},),
    ({**RESULT, 'content': [{'text': 123}]},),
    ({**RESULT, 'content': [{'text': 'yes', 'json': {}}]},),
    ({**RESULT, 'content': [{'json': {1, 2}}]},),
    ({**RESULT, 'type': 'tool_result'},),
])
def test_pending_turn_rejects_missing_duplicate_or_malformed_actual_results(results):
    store = MemoryStore()
    harness = HarnessBoundary(store, HarnessReply('Approve', 'tool_use', (PENDING,)))
    app = CloudWorkflow(store, harness)
    started = app.start(CONTEXT, 'Inspect cache')
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        app.resume(CONTEXT, started.task_id, 'I approve', results)
    assert len(harness.calls) == len(store.turns) == 1
    assert store.tasks[started.task_id].pending_tools == (PENDING,)
    assert store.jobs == {}


def test_nonwaiting_turn_rejects_tool_results_without_invocation():
    store = MemoryStore()
    harness = HarnessBoundary(store, HarnessReply('A response', 'end_turn'))
    app = CloudWorkflow(store, harness)
    started = app.start(CONTEXT, 'Inspect cache')
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        app.resume(CONTEXT, started.task_id, tool_results=(RESULT,))
    assert len(harness.calls) == 1


@pytest.mark.parametrize('changed', [
    {'actor_id': ''}, {'project_id': '  '}, {'service_id': ''}, {'source_ids': frozenset({''})},
])
def test_invalid_access_context_cannot_create_a_task(changed):
    store = MemoryStore()
    harness = HarnessBoundary(store)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        CloudWorkflow(store, harness).start(replace(CONTEXT, **changed), 'cache')
    assert store.tasks == {}
    assert harness.calls == []


def test_mutating_original_grants_cannot_add_sources_to_an_existing_task():
    original = {'repository'}
    context = AccessContext('alice', 'project', 'cache', original)
    store = MemoryStore()
    store.memories['restricted'] = (
        SharedMemory('restricted', 'Restricted procedure', frozenset({'restricted'})), CONTEXT)
    harness = HarnessBoundary(store, HarnessReply('A response', 'end_turn'), HarnessReply('Continued', 'end_turn'))
    app = CloudWorkflow(store, harness)
    first = app.start(context, 'cache')
    original.add('restricted')

    resumed = app.resume(context, first.task_id, 'Continue')
    assert resumed.recalled_ids == ()
    assert store.tasks[first.task_id].context.source_ids == frozenset({'repository'})


@pytest.mark.parametrize('prompt', ['', ' \n', None, 12])
def test_start_requires_a_real_prompt_before_creating_task(prompt):
    store = MemoryStore()
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        CloudWorkflow(store, HarnessBoundary(store)).start(CONTEXT, prompt)
    assert store.tasks == {}


@pytest.mark.parametrize('changed', [
    {'actor_id': 'bob'}, {'project_id': 'other'}, {'service_id': 'other'}, {'source_ids': frozenset()},
])
def test_resume_checks_current_context_before_reusing_a_session(changed):
    store = MemoryStore()
    harness = HarnessBoundary(store, HarnessReply('A response', 'end_turn'))
    app = CloudWorkflow(store, harness)
    started = app.start(CONTEXT, 'cache')
    with pytest.raises(StoreError, match='ACCESS_DENIED'):
        app.resume(replace(CONTEXT, **changed), started.task_id, 'Continue')
    assert len(harness.calls) == 1


def test_query_embedding_failure_continues_with_authorized_keyword_recall():
    store = MemoryStore()
    store.memories['memory'] = (SharedMemory('memory', 'Cache procedure', CONTEXT.source_ids), CONTEXT)
    harness = HarnessBoundary(store, HarnessReply('Using a recalled procedure', 'end_turn'))
    result = CloudWorkflow(store, harness, EmbedderBoundary(fail_query=True)).start(CONTEXT, 'cache')
    assert result.recalled_ids == ('memory',)
    assert store.searches[0][2:4] == (None, None)
    recalled = memory_context(harness.calls[0][1])
    assert recalled['retrieval_state'] == 'embedding_unavailable'
    assert 'private-query-token' not in json.dumps(harness.calls[0][1])


def test_hybrid_recall_passes_matching_model_identity_to_store():
    store = MemoryStore()
    harness = HarnessBoundary(store, HarnessReply('Plan', 'end_turn'))
    CloudWorkflow(store, harness, EmbedderBoundary()).start(CONTEXT, 'cache')
    assert store.searches[0][2:4] == ([0.25, 0.75], 'fixture:model-v1')
    assert memory_context(harness.calls[0][1])['retrieval_state'] == 'hybrid'


def test_busy_task_prevents_a_second_external_invocation():
    store = MemoryStore()
    task = store.create_task(CONTEXT)
    store.busy.add(task.id)
    harness = HarnessBoundary(store)
    with pytest.raises(StoreError, match='TASK_BUSY'):
        CloudWorkflow(store, harness).resume(CONTEXT, task.id, 'Continue')
    assert harness.calls == []
    assert store.turns == []


@pytest.mark.parametrize('reply', [
    HarnessReply('Upstream unavailable', 'error', error='HARNESS_UNAVAILABLE'),
    RuntimeError('private-harness-token'),
])
def test_harness_failure_is_captured_once_without_exposing_unexpected_exception(reply):
    store = MemoryStore()
    harness = HarnessBoundary(store, reply)
    result = CloudWorkflow(store, harness).start(CONTEXT, 'cache')
    assert result.reply.error == 'HARNESS_UNAVAILABLE'
    assert 'private-harness-token' not in repr(result)
    assert len(harness.calls) == len(store.turns) == 1
    assert store.events == ['begin', 'invoke', 'finish']
    assert store.jobs == {}


def test_capture_failure_never_reinvokes_a_potentially_completed_external_action():
    store = MemoryStore()
    store.fail_capture = True
    harness = HarnessBoundary(store, HarnessReply('Already performed', 'end_turn'))
    with pytest.raises(StoreError, match='CAPTURE_FAILED'):
        CloudWorkflow(store, harness).start(CONTEXT, 'cache')
    assert len(harness.calls) == 1
    assert store.turns == []
    assert store.busy == {'1'}


def test_indexing_retry_preserves_searchable_text_and_does_not_reextract():
    store = MemoryStore()
    harness = HarnessBoundary(store, HarnessReply('Cache verified', 'end_turn'))
    turn = CloudWorkflow(store, harness).start(CONTEXT, 'Inspect cache')
    extractor = ExtractorBoundary()
    embedder = EmbedderBoundary(store, fail_document=True)
    worker = SharedKnowledgeProcessor(store, extractor, embedder)

    assert worker.process_next() is True
    assert store.jobs[turn.turn_id].text == 'Compare cache values using the same identifier.'
    assert store.search(CONTEXT, 'cache', None, None, 5)[0].id == turn.turn_id
    assert store.events[-2:] == ['save_text', ('retry', 'EMBEDDING_FAILED')]
    embedder.fail_document = False
    assert worker.process_next() is True
    assert extractor.calls == [('Inspect cache', 'Cache verified')]
    assert store.events[-1] == ('complete', [0.5, 0.5], 'fixture:model-v1')
    assert store.jobs == {}
    assert worker.process_next() is False


def test_extraction_failure_is_retryable_without_private_exception_text():
    store = MemoryStore()
    store.jobs['job'] = KnowledgeJob('job', 'token', CONTEXT, 'cache', 'Verified')
    worker = SharedKnowledgeProcessor(store, ExtractorBoundary(RuntimeError('private-extractor-token')))
    assert worker.process_next() is True
    assert store.jobs['job'].text is None
    assert store.events == [('retry', 'EXTRACTION_FAILED')]
    assert store.memories == {}


@pytest.mark.parametrize('text', ['', '  \n'])
def test_empty_experience_finishes_job_without_inserting_empty_knowledge(text):
    store = MemoryStore()
    store.jobs['job'] = KnowledgeJob('job', 'token', CONTEXT, 'cache', 'Verified')
    assert SharedKnowledgeProcessor(store, ExtractorBoundary(text), EmbedderBoundary(store)).process_next() is True
    assert store.jobs == {}
    assert store.memories == {}
    assert store.events == [('complete', None, None)]


def test_keyword_only_processor_completes_without_an_embedding_provider():
    store = MemoryStore()
    store.jobs['job'] = KnowledgeJob('job', 'token', CONTEXT, 'cache', 'Verified')
    assert SharedKnowledgeProcessor(store, ExtractorBoundary()).process_next() is True
    assert store.events == ['save_text', ('complete', None, None)]
    assert store.memories['job'][0].text == 'Compare cache values using the same identifier.'
