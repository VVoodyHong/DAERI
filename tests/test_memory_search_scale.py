"""Search large canonical stores without an arbitrary prefix or giant SQL bindings."""

from contextlib import closing
import json
import sqlite3
import struct

import pytest

from daeri.infrastructure.memory import MemoryService
from daeri.infrastructure.storage import Store
from test_embedding_memory import api, api_config
from test_memory import (NumericalFixtureEmbedder, TARGET_HASH, candidate,
                         configured_resource, memory_policy, save)


@pytest.fixture
def corpus(tmp_path):
    with closing(Store(tmp_path)) as store:
        def create(mode='keyword'):
            embedding = api_config() if mode == 'api' else {'provider': 'local' if mode == 'vector' else 'keyword_only'}
            profile = store.put_profile('alpha', resources=[configured_resource()],
                                        memory_policy=memory_policy(), embedding=embedding)
            return MemoryService(store, NumericalFixtureEmbedder() if mode == 'vector' else None,
                                 auto_load=False), profile
        yield create


def populate(service, profile, count, *, blocked=False, filler='unrelated observations'):
    # Clone one validated observation to seed scale; save/lineage validation has separate regressions.
    seed = save(service, profile, draft=candidate(filler))
    db = service.store._db
    row = db.execute('SELECT * FROM memory_versions WHERE memory_ref=?', (seed['memory_ref'],)).fetchone()
    vector = db.execute('SELECT * FROM memory_vectors WHERE memory_ref=?', (seed['memory_ref'],)).fetchone()
    body = json.loads(row['body'])
    target = 'memory_zzzz_related'
    with service.store.transaction():
        service._drop_indexes(seed['memory_ref'])
        db.execute('DELETE FROM memory_versions WHERE memory_ref=?', (seed['memory_ref'],))
        db.execute('DELETE FROM memory_heads WHERE memory_id=?', (row['memory_id'],))
        for index in range(count + 1):
            reference = f'memory_{index:08d}' if index < count else target
            item = {**body, 'memory_ref': reference,
                    'statement': filler if index < count else 'NEEDLE42 연결 문제 해결'}
            if blocked and index < count:
                item['_target_bindings'] = {'primary-db': 'obsolete-target'}
            db.execute('INSERT INTO memory_heads VALUES(?,?,?,0)', (reference, profile['profile_id'], reference))
            db.execute('''INSERT INTO memory_versions
                (memory_ref,memory_id,profile_id,version,body,content_hash,source_hash,
                 created_at,expires_at,index_state,source_lineage) VALUES(?,?,?,1,?,?,?,?,?,?,?)''',
                (reference, reference, profile['profile_id'], json.dumps(item), row['content_hash'],
                 row['source_hash'], row['created_at'], row['expires_at'], row['index_state'], row['source_lineage']))
            db.execute('INSERT INTO memory_fts VALUES(?,?)', (reference, item['statement']))
            if vector:
                packed = struct.pack('<3f', *([1, 0, 0] if blocked or index == count else [0, 1, 0]))
                db.execute('INSERT INTO memory_vectors VALUES(?,?,?,?)',
                           (reference, vector['model_id'], vector['dimensions'], packed))
    return target


@pytest.mark.parametrize('mode', ['keyword', 'vector'])
def test_related_memory_beyond_ten_thousand_is_searchable(corpus, mode):
    service, profile = corpus(mode)
    target = populate(service, profile, 10_000)
    query = 'NEEDLE42' if mode == 'keyword' else 'semantic-fixture-query'
    result = service.search(profile['profile_id'], query, limit=1)
    assert [item['memory_ref'] for item in result['items']] == [target]
    assert result['limitations'] == []


@pytest.mark.parametrize('mode', ['keyword', 'vector'])
def test_ranked_candidates_continue_past_blocked_batches(corpus, mode):
    service, profile = corpus(mode)
    target = populate(service, profile, 150, blocked=True, filler='NEEDLE42')
    result = service.search(profile['profile_id'], 'NEEDLE42' if mode == 'keyword' else 'semantic-fixture-query', limit=1)
    assert [item['memory_ref'] for item in result['items']] == [target]
    assert result['limitations'] == []


def test_search_does_not_bind_or_parse_the_entire_corpus(corpus, monkeypatch):
    service, profile = corpus()
    target = populate(service, profile, 250)
    checked = []
    allowed = service._allowed
    def inspect(item, *args, **kwargs):
        checked.append(item['memory_ref'])
        return allowed(item, *args, **kwargs)
    monkeypatch.setattr(service, '_allowed', inspect)
    previous = service.store._db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
    try:
        result = service.search(profile['profile_id'], 'NEEDLE42', limit=1)
    finally:
        service.store._db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
    assert [item['memory_ref'] for item in result['items']] == [target]
    assert result['limitations'] == []
    assert len(checked) <= 10


def test_api_checks_allowed_memory_beyond_the_old_prefix(corpus, api):
    service, profile = corpus('api')
    target = populate(service, profile, 10_000, blocked=True)
    api.calls.clear()
    result = service.search(profile['profile_id'], 'semantic-fixture-query', limit=1)
    assert [item['memory_ref'] for item in result['items']] == [target]
    assert [call[0] for call in api.calls] == ['query']
    assert result['limitations'] == []


def test_api_rechecks_other_allowed_memories_if_the_initial_candidate_is_invalidated(corpus, api, monkeypatch):
    service, profile = corpus('api')
    target = populate(service, profile, 1)
    selected = service._selected_embedder
    def invalidate_first(current):
        embedder = selected(current)
        sources = json.loads(service.store._db.execute(
            'SELECT body FROM memory_versions WHERE memory_ref=?', ('memory_00000000',)).fetchone()[0])['_sources']
        service.invalidate(profile['profile_id'], 'memory_00000000', reason='새 관찰로 첫 기억을 정정', source_records=sources)
        return embedder
    monkeypatch.setattr(service, '_selected_embedder', invalidate_first)
    api.calls.clear()
    result = service.search(profile['profile_id'], 'semantic-fixture-query', limit=1)
    assert [item['memory_ref'] for item in result['items']] == [target]
    assert [call[0] for call in api.calls] == ['query']
    assert result['limitations'] == []


def test_selected_resource_is_searchable_after_a_large_other_resource(corpus):
    service, profile = corpus()
    target = populate(service, profile, 10_000)
    secondary = {**configured_resource(), 'instance_id': 'archive-db'}
    service.store.put_profile('alpha', resources=[configured_resource(), secondary],
        memory_policy=memory_policy(instance_ids=['primary-db', 'archive-db'],
            target_bindings={'primary-db': TARGET_HASH, 'archive-db': TARGET_HASH}))
    with service.store.transaction():
        service.store._db.execute('''UPDATE memory_versions SET body=json_set(body,
            '$.instance_ids',json('["archive-db"]'),
            '$._target_bindings',json(?),
            '$._sources[0].instance_ids',json('["archive-db"]'),
            '$._sources[0].target_bindings',json(?)) WHERE memory_ref!=?''',
            [json.dumps({'archive-db': TARGET_HASH}), json.dumps({'archive-db': TARGET_HASH}), target])
    result = service.search(profile['profile_id'], 'NEEDLE42', instance_ids=['primary-db'], limit=1)
    assert [item['memory_ref'] for item in result['items']] == [target]
    assert result['limitations'] == []


@pytest.mark.parametrize('mode', ['keyword', 'vector'])
def test_unavailable_indexes_of_blocked_memories_do_not_report_partial_search(corpus, mode):
    service, profile = corpus(mode)
    target = populate(service, profile, 150, blocked=True)
    table = 'memory_fts' if mode == 'keyword' else 'memory_vectors'
    service.store._db.execute(f'DELETE FROM {table} WHERE memory_ref!=?', (target,))
    result = service.search(profile['profile_id'], 'NEEDLE42' if mode == 'keyword' else 'semantic-fixture-query', limit=1)
    assert [item['memory_ref'] for item in result['items']] == [target]
    assert result['limitations'] == []
