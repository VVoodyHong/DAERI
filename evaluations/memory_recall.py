"""Repeat the fixed synthetic recall benchmark against the actual memory service."""

import argparse
from datetime import UTC, datetime
import hashlib
from itertools import count
import json
import math
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from uuid import UUID

from daeri.infrastructure.embedding import validate_embedding_config
from daeri.infrastructure.memory import LocalEmbedder, MemoryService
from daeri.core.policy import effective_profile_memory_policy, effective_resource_policy
from daeri.config.setup_yaml import read_yaml
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


DATASET = Path(__file__).with_suffix('.json')
GROUPS = {'identifier', 'paraphrase', 'multi_evidence', 'correction', 'unrelated', 'isolation'}


def load_dataset():
    data = json.loads(DATASET.read_text(encoding='utf-8'))
    _validate_dataset(data)
    return data


def _validate_dataset(data):
    if (data.get('version') != 1 or not data.get('memories') or not data.get('queries')):
        raise ValueError('Invalid evaluation dataset')
    memories, superseded = {}, set()
    for item in data['memories']:
        if (not all(isinstance(item.get(key), str) and item[key].strip()
                    for key in ('id', 'statement', 'applicability'))
                or item['id'] in memories or item['profile'] not in ('primary', 'other')
                or item['access'] not in ('allowed', 'revoked')):
            raise ValueError('Invalid evaluation memory')
        if 'replaces' in item:
            previous = memories.get(item['replaces'])
            if (previous is None or previous['profile'] != item['profile']
                    or previous['access'] != item['access'] or previous['id'] in superseded):
                raise ValueError('Invalid evaluation correction')
            superseded.add(previous['id'])
        memories[item['id']] = item
    query_ids = set()
    for case in data['queries']:
        if (not isinstance(case.get('id'), str) or not case['id'] or case['id'] in query_ids
                or not isinstance(case.get('text'), str) or not case['text'].strip()
                or case.get('group') not in GROUPS or not isinstance(case.get('expected'), list)
                or len(case['expected']) != len(set(case['expected']))):
            raise ValueError('Invalid evaluation query')
        query_ids.add(case['id'])
        for expected in case['expected']:
            item = memories.get(expected)
            if (item is None or expected in superseded or item['profile'] != 'primary'
                    or item['access'] != 'allowed'):
                raise ValueError('Expected evidence must be current and accessible')


def score_case(expected, retrieved, top_k):
    ranks = {key: next((rank for rank, item in enumerate(retrieved[:top_k], 1)
                        if item == key), None) for key in expected}
    hits = [rank for rank in ranks.values() if rank is not None]
    return {'recall_at_k': len(hits) / len(ranks) if ranks else None,
            'reciprocal_rank': 1 / min(hits) if hits else 0 if ranks else None,
            'expected_ranks': ranks}


def _resource(instance):
    return {'instance_id': instance, 'type': 'Document', 'adapter': 'document_read',
            'target': {'server': 'https://source.example.test', 'project': instance},
            'identity_ref': 'test:synthetic',
            'connection': {'transport': 'stdio', 'command': sys.executable, 'args': ['-c', 'pass']},
            'capabilities': [{'capability_id': 'read', 'tool_name': 'confluence_get_page', 'constraints': {}}]}


def _seed(service, dataset, embedding):
    store = service.store
    resources = [_resource('allowed'), _resource('revoked')]
    profiles = {name: store.put_profile('eval-' + name, resources=resources, embedding=embedding)
                for name in ('primary', 'other')}
    references, index_states = {}, []
    for item in dataset['memories']:
        profile = profiles[item['profile']]
        instance = 'revoked' if item['access'] == 'revoked' else 'allowed'
        policy = effective_profile_memory_policy(profile)
        now = datetime.now(UTC).isoformat()
        task_id = 'eval-' + item['id']
        store.insert_task({'task_id': task_id, 'profile_id': profile['profile_id'],
                           'task_revision': 1, 'state': 'succeeded', 'created_at': now})
        record = store.write_record(task_id, 'request', {'text': item['statement']}, None)
        source = {'task_id': task_id, 'record_ref': record,
                  'fields': [{'segments': [{'key': 'text'}]}], 'instance_ids': [instance],
                  'target_bindings': {instance: policy['target_bindings'][instance]},
                  'verified_at': now, 'expires_at': None, 'available': True,
                  'model_allowed': True, 'retain_allowed': True, 'long_term_allowed': True}
        candidate = {'category': 'lesson', 'statement': item['statement'],
                     'applicability': item['applicability'], 'instance_ids': [instance],
                     'verification_method': '고정 합성 자료의 확인된 요청과 대조'}
        saved = service.save(profile['profile_id'], candidate, source_records=[source],
                             policy={**policy, 'retention': 'source_bound', 'expires_at': None},
                             memory_ref=references.get(item.get('replaces')))
        references[item['id']] = saved['memory_ref']
        index_states.append(saved['index_state'])
    # Keep the old index present so current permission checks are exercised at query time.
    resources[1]['policy'] = effective_resource_policy(resources[1])
    resources[1]['policy']['memory']['allow_model_recall'] = False
    store.put_profile('eval-primary', resources=resources, embedding=embedding)
    superseded = {item['replaces'] for item in dataset['memories'] if 'replaces' in item}
    permitted = {item['id'] for item in dataset['memories'] if item['profile'] == 'primary'
                 and item['access'] == 'allowed' and item['id'] not in superseded}
    return profiles['primary']['profile_id'], references, permitted, all(s == 'ready' for s in index_states)


def _metrics(cases, complete):
    positive = [case for case in cases if case['expected']]
    times = sorted(case['search_ms'] for case in cases)
    return {'positive_queries': len(positive), 'no_gold_queries': len(cases) - len(positive),
            'recall_at_k': sum(case['recall_at_k'] for case in positive) / len(positive)
                           if complete and positive else None,
            'mrr_at_k': sum(case['reciprocal_rank'] for case in positive) / len(positive)
                        if complete and positive else None,
            'forbidden_results': sum(len(case['forbidden']) for case in cases),
            'search_ms': {'mean': round(sum(times) / len(times), 3),
                          'p95': times[math.ceil(len(times) * 0.95) - 1]}}


def evaluate(dataset, embedding, *, model_cache=None, top_k=5):
    _validate_dataset(dataset)
    if type(top_k) is not int or not 1 <= top_k <= 50:
        raise ValueError('top_k must be between 1 and 50')
    embedding = validate_embedding_config(embedding)
    provider = embedding['provider']
    embedder = LocalEmbedder.load(model_cache) if provider == 'local' and model_cache else None
    if provider == 'local' and embedder is None:
        raise StoreError('MEMORY_SEARCH_INCOMPLETE', 'A prepared model cache is required.')
    with TemporaryDirectory(prefix='daeri-recall-evaluation-') as directory:
        store = Store(Path(directory))
        try:
            service = MemoryService(store, embedder=embedder, auto_load=False)
            # Canonical tie breaks use refs; stable fixture UUIDs make repeated rankings comparable.
            sequence = count(1)
            with patch('daeri.infrastructure.memory.uuid4', side_effect=lambda: UUID(int=next(sequence))):
                profile_id, references, permitted, indexed = _seed(service, dataset, embedding)
            names = {reference: name for name, reference in references.items()}
            cases = []
            for case in dataset['queries']:
                started = perf_counter()
                result = service.search(profile_id, case['text'], limit=top_k)
                elapsed = round((perf_counter() - started) * 1000, 3)
                retrieved = [names.get(item['memory_ref'], '<unknown>') for item in result['items']]
                cases.append({**case, 'retrieved': retrieved,
                              **score_case(case['expected'], retrieved, top_k),
                              'forbidden': [name for name in retrieved if name not in permitted],
                              'search_ms': elapsed, 'retrieval_state': result['retrieval_state'],
                              'limitations': result['limitations']})
            expected_mode = 'keyword_only' if provider == 'keyword_only' else 'hybrid'
            complete = indexed and all(case['retrieval_state'] == expected_mode
                                       and not case['limitations'] for case in cases)
            if not complete:
                for case in cases:
                    case.update(recall_at_k=None, reciprocal_rank=None)
            metrics = _metrics(cases, complete)
            return {'report_version': 1, 'dataset_version': dataset['version'],
                    'dataset_sha256': hashlib.sha256(json.dumps(dataset, ensure_ascii=False,
                        sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                    'provider': provider, 'evaluation_kind': 'keyword_baseline'
                        if provider == 'keyword_only' else 'semantic_recall',
                    'model_id': service.status(profile_id)['model_id'],
                    'python_version': sys.version.split()[0], 'sqlite_version': sqlite3.sqlite_version,
                    'top_k': top_k, 'status': 'complete' if complete else 'incomplete',
                    'index_complete': indexed, 'integrity_passed': metrics['forbidden_results'] == 0,
                    'metrics': metrics,
                    'groups': {group: _metrics([case for case in cases if case['group'] == group], complete)
                               for group in sorted({case['group'] for case in cases})},
                    'cases': cases,
                    'limitations': ['Fixed synthetic corpus; real task and host quality require separate evaluation.',
                        'No-gold cases are excluded from recall/MRR; allowed nearest results can still appear.',
                        'Search timing includes query embedding, excludes corpus indexing and model loading.']}
        finally:
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=['keyword_only', 'local', 'api'], default='keyword_only')
    parser.add_argument('--model-cache', type=Path, help='Prepared local model directory (read only)')
    parser.add_argument('--embedding-config', type=Path, help='YAML embedding configuration with credential references')
    parser.add_argument('--top-k', type=int, default=5)
    parser.add_argument('--output', type=Path, help='Also save the JSON report to this file')
    args = parser.parse_args(argv)
    try:
        if ((args.provider == 'local') != (args.model_cache is not None)
                or (args.provider == 'api') != (args.embedding_config is not None)):
            raise ValueError('Provider arguments do not match')
        config = {'provider': args.provider}
        if args.embedding_config:
            value = read_yaml(args.embedding_config)
            config = validate_embedding_config(value.get('embedding', value))
            if config['provider'] != 'api':
                raise ValueError('API configuration is required')
        report = evaluate(load_dataset(), config, model_cache=args.model_cache, top_k=args.top_k)
    except (StoreError, OSError, ValueError, ImportError, RuntimeError, sqlite3.Error) as error:
        report = {'status': 'unavailable', 'provider': args.provider,
                  'code': error.code if isinstance(error, StoreError) else 'EVALUATION_UNAVAILABLE',
                  'message': '평가 입력·준비된 모델·임베딩 연결을 확인한 뒤 다시 실행하세요.'}
    output = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        try:
            args.output.write_text(output, encoding='utf-8')
        except OSError:
            print(json.dumps({'status': 'unavailable', 'code': 'REPORT_WRITE_FAILED'}))
            return 1
    print(output, end='')
    return 0 if report['status'] == 'complete' and report['integrity_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
