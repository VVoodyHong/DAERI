"""Durable report follow-up; task revisions remain those of user-visible mutations."""

import copy
import sqlite3
from datetime import UTC, datetime

from .knowledge import update
from .security import redact
from .storage import StoreError


_RETRY = {'STORE_UNAVAILABLE', 'RECORD_UNAVAILABLE', 'MEMORY_INDEX_INCOMPLETE'}


def _fallback(engine, task, plan, report):
    if not plan:
        return []
    rules = {rule['instance_id']: rule['memory'] for rule in plan['draft']['output']['resource_rules']}
    criteria = {item['criterion_id']: item for item in plan['draft']['completion_criteria']}
    verified = {step for check in report.get('checks', []) if check.get('status') == 'met'
                for step in criteria.get(check['criterion_id'], {}).get('step_ids', [])}
    resources = {item['instance_id']: item for item in task['profile_snapshot']['resources']}
    sources, instances, methods = [], set(), set()
    for call in task['calls']:
        if len(sources) >= 100:
            break
        memory = rules.get(call['instance_id'], {})
        if (call['step_id'] not in verified or call['call_state'] != 'completed'
                or call.get('error_code') or not call.get('result_ref')
                or 'procedure' not in memory.get('categories', []) or memory.get('retention') == 'none'):
            continue
        try:
            evidence = engine._load_evidence(task, call['result_ref'])
        except StoreError:
            continue
        if evidence.get('source_complete') is not True or not evidence.get('provided_paths'):
            continue
        sources.append(call['result_ref'])
        instances.add(call['instance_id'])
        methods.add(resources[call['instance_id']]['type'] + ' ' + call['capability_id'])
    if not sources:
        return []
    return [{'draft': {'category': 'procedure',
        'statement': '검증한 수행 방법: ' + ', '.join(sorted(methods)) + '. 승인 범위를 조회하고 완료 조건을 확인했다.',
        'applicability': '같은 profile과 실제 대상의 동일 기능을 이용한 업무. 재실행 전에 현재 계약과 범위를 확인한다.',
        'instance_ids': sorted(instances), 'verification_method': '보존된 성공 호출과 완료 검사 대조'},
        'source_refs': list(dict.fromkeys(sources))[:100]}]


def enqueue(engine, task, plan, args, report_ref):
    candidates = args.get('memory_candidates') or _fallback(engine, task, plan, args['report'])
    items = []
    for candidate in candidates:
        if redact(candidate) != candidate:
            items.append({'state': 'skipped', 'error_code': 'RESULT_VALIDATION_FAILED'})
        else:
            try:
                update(engine, {'operation': 'store', **candidate, 'reason': '보고 후 기억 후보 검증'},
                       task, automatic=True, validate_only=True)
            except (StoreError, OSError, sqlite3.Error) as error:
                code = error.code if isinstance(error, StoreError) else 'STORE_UNAVAILABLE'
                items.append({'state': 'pending' if code in _RETRY else 'skipped', 'error_code': code})
            else:
                items.append({'state': 'pending', 'candidate': copy.deepcopy(candidate)})
    if not items:
        return
    ref = engine.store.write_record(task['task_id'], 'memory_pending',
                                    {'report_ref': report_ref, 'items': items})
    task.setdefault('memory_job_refs', []).append(ref)


def _publish(engine, task_id, ref, payload):
    engine.store.write_memory_job(task_id, ref, payload,
        done=not any(item['state'] == 'pending' for item in payload['items']))


def _run(engine, task_id, ref):
    with engine.store.transaction():
        payload = engine.store.read_record(task_id, ref)['payload']
    for index, initial in enumerate(payload['items']):
        if initial['state'] != 'pending':
            continue
        try:
            with engine.store.transaction():
                task = engine.store.get_task(task_id)
                payload = engine.store.read_record(task_id, ref)['payload']
                item = payload['items'][index]
                if item['state'] != 'pending':
                    continue
                if 'memory_ref' not in item:
                    candidate = item.get('candidate')
                    if candidate is None:
                        report = engine.store.read_record(task_id, payload['report_ref'])['payload']
                        candidates = _fallback(engine, task, engine._current_plan(task), report)
                        if not candidates:
                            item.update(state='skipped', error_code='RESULT_VALIDATION_FAILED')
                            _publish(engine, task_id, ref, payload)
                            continue
                        candidate = candidates[0]
                    result = update(engine, {'operation': 'store', **candidate,
                        'source_refs': candidate['source_refs'], 'reason': '검증한 보고 후 자동 기억'},
                        task, automatic=True)
                    item['memory_ref'] = result['data']['memory_ref']
                    item.pop('candidate', None)
                    _publish(engine, task_id, ref, payload)
                engine.memory.get(task['profile_id'], item['memory_ref'])
                profile_id, memory_ref = task['profile_id'], item['memory_ref']
            state = engine.memory._index(profile_id, memory_ref)
            with engine.store.transaction():
                payload = engine.store.read_record(task_id, ref)['payload']
                item = payload['items'][index]
                item['state'] = 'ready' if state == 'ready' else 'pending'
                item.pop('error_code', None)
                if state != 'ready':
                    item['error_code'] = 'MEMORY_INDEX_INCOMPLETE'
                _publish(engine, task_id, ref, payload)
            if state != 'ready':
                return False
        except (StoreError, OSError, sqlite3.Error) as error:
            code = error.code if isinstance(error, StoreError) else 'STORE_UNAVAILABLE'
            try:
                with engine.store.transaction():
                    payload = engine.store.read_record(task_id, ref)['payload']
                    item = payload['items'][index]
                    item.update(state='pending' if code in _RETRY else 'skipped', error_code=code)
                    if item['state'] == 'skipped':
                        item.pop('candidate', None)
                    _publish(engine, task_id, ref, payload)
            except (StoreError, OSError, sqlite3.Error):
                pass
            if code in _RETRY:
                return False
    with engine.store.transaction():
        payload = engine.store.read_record(task_id, ref)['payload']
        _publish(engine, task_id, ref, payload)
    return True


def _summary(items):
    public = [{key: value for key, value in item.items() if key != 'candidate'} for item in items]
    states = {item['state'] for item in public}
    state = ('skipped' if not states or states == {'skipped'} else 'ready' if states == {'ready'}
             else 'pending' if states == {'pending'} else 'partial')
    return {'state': state, 'items': public}


def finish_report(engine, receipt):
    task_id, report_ref = receipt['task_id'], receipt['report_ref']
    try:
        task = engine.store.get_task(task_id)
        refs = []
        for ref in task.get('memory_job_refs', []):
            job = engine.store.read_record(task_id, ref)
            if job['payload']['report_ref'] == report_ref:
                refs.append(ref)
                if job['record_kind'] == 'memory_pending':
                    _run(engine, task_id, ref)
        items = [copy.deepcopy(item) for ref in refs
                 for item in engine.store.read_record(task_id, ref)['payload']['items']]
        for item in items:
            if item['state'] == 'ready':
                try:
                    engine.memory.get(task['profile_id'], item['memory_ref'])
                except StoreError as error:
                    item.update(state='skipped', error_code=error.code)
        return _summary(items)
    except (StoreError, OSError, sqlite3.Error) as error:
        code = error.code if isinstance(error, StoreError) else 'STORE_UNAVAILABLE'
        state = 'pending' if code in _RETRY else 'skipped'
        return {'state': state, 'items': [{'state': state, 'error_code': code}]}


def retry_profile(engine, profile_id):
    if not profile_id:
        return
    with engine.store.transaction():
        now = datetime.now(UTC).isoformat()
        engine.store._db.execute("""UPDATE records SET kind='memory_done' WHERE kind='memory_pending'
            AND expires_at IS NOT NULL AND expires_at<=?
            AND task_id IN (SELECT task_id FROM tasks WHERE profile_id=?)""", (now, profile_id))
        pending = engine.store._db.execute("""SELECT r.task_id,r.record_ref FROM records r JOIN tasks t USING(task_id)
            WHERE t.profile_id=? AND r.kind='memory_pending' AND r.blocked=0
            ORDER BY r.rowid LIMIT 5""", (profile_id,)).fetchall()
    for row in pending:
        try:
            if not _run(engine, row['task_id'], row['record_ref']):
                break
        except (StoreError, OSError, sqlite3.Error):
            break
