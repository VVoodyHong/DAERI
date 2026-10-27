"""Durable report follow-up; task revisions remain those of user-visible mutations."""

import copy
import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from daeri.application.knowledge import update
from daeri.core.contracts import ReportMemoryCandidate
from daeri.core.security import redact
from daeri.core.errors import StoreError


_RETRY = {'STORE_UNAVAILABLE', 'RECORD_UNAVAILABLE', 'MEMORY_INDEX_INCOMPLETE'}


def _candidate(runtime, task, candidate):
    try:
        candidate = ReportMemoryCandidate.model_validate(candidate).model_dump(exclude_unset=True)
    except ValidationError:
        return {'state': 'skipped', 'error_code': 'INVALID_INPUT'}
    if redact(candidate) != candidate:
        return {'state': 'skipped', 'error_code': 'RESULT_VALIDATION_FAILED'}
    try:
        update(runtime, {'operation': 'store', **candidate, 'reason': '보고 후 기억 후보 검증'},
               task, automatic=True, validate_only=True)
    except (StoreError, OSError, sqlite3.Error) as error:
        code = error.code if isinstance(error, StoreError) else 'STORE_UNAVAILABLE'
        return {'state': 'skipped', 'error_code': code, **({'next_action': 'resubmit'} if code in _RETRY else {})}
    return {'state': 'pending', 'candidate': copy.deepcopy(candidate)}


def enqueue(runtime, task, args, report_ref):
    items = [_candidate(runtime, task, candidate) for candidate in args.get('memory_candidates', [])]
    if not items:
        return
    try:
        with runtime.store.transaction():
            runtime.store._db.execute('SAVEPOINT report_memory')
            try:
                ref = runtime.store.write_record(task['task_id'], 'memory_pending',
                                                {'report_ref': report_ref, 'items': items})
            except (StoreError, OSError, sqlite3.Error):
                runtime.store._db.execute('ROLLBACK TO report_memory')
                raise
            finally:
                runtime.store._db.execute('RELEASE report_memory')
    except (StoreError, OSError, sqlite3.Error) as error:
        code = error.code if isinstance(error, StoreError) else 'STORE_UNAVAILABLE'
        return {'state': 'skipped', 'items': [
            {'state': 'skipped', 'error_code': code, **({'next_action': 'resubmit'} if code in _RETRY else {})}]}
    task.setdefault('memory_job_refs', []).append(ref)


def _publish(runtime, task_id, ref, payload):
    runtime.store.write_memory_job(task_id, ref, payload,
        done=not any(item['state'] == 'pending' for item in payload['items']))


def _run(runtime, task_id, ref):
    with runtime.store.transaction():
        payload = runtime.store.read_record(task_id, ref)['payload']
    for index, initial in enumerate(payload['items']):
        if initial['state'] != 'pending':
            continue
        try:
            with runtime.store.transaction():
                task = runtime.store.get_task(task_id)
                payload = runtime.store.read_record(task_id, ref)['payload']
                item = payload['items'][index]
                if item['state'] != 'pending':
                    continue
                if 'memory_ref' not in item:
                    candidate = item.get('candidate')
                    if candidate is None:
                        item.update(state='skipped', error_code=item.get('error_code', 'RESULT_VALIDATION_FAILED'),
                                    next_action='resubmit')
                        _publish(runtime, task_id, ref, payload)
                        continue
                    result = update(runtime, {'operation': 'store', **candidate, 'reason': '검증한 보고 후 자동 기억'},
                        task, automatic=True)
                    item['memory_ref'] = result['data']['memory_ref']
                    item.pop('candidate', None)
                    _publish(runtime, task_id, ref, payload)
                runtime.memory.get(task['profile_id'], item['memory_ref'])
                profile_id, memory_ref = task['profile_id'], item['memory_ref']
            state = runtime.memory._index(profile_id, memory_ref)
            with runtime.store.transaction():
                payload = runtime.store.read_record(task_id, ref)['payload']
                item = payload['items'][index]
                item['state'] = 'ready' if state == 'ready' else 'pending'
                item.pop('error_code', None)
                if state != 'ready':
                    item['error_code'] = 'MEMORY_INDEX_INCOMPLETE'
                _publish(runtime, task_id, ref, payload)
            if state != 'ready':
                return False
        except (StoreError, OSError, sqlite3.Error) as error:
            code = error.code if isinstance(error, StoreError) else 'STORE_UNAVAILABLE'
            try:
                with runtime.store.transaction():
                    payload = runtime.store.read_record(task_id, ref)['payload']
                    item = payload['items'][index]
                    item.update(state='pending' if code in _RETRY else 'skipped', error_code=code)
                    if item['state'] == 'skipped':
                        item.pop('candidate', None)
                    _publish(runtime, task_id, ref, payload)
            except (StoreError, OSError, sqlite3.Error):
                pass
            if code in _RETRY:
                return False
    with runtime.store.transaction():
        payload = runtime.store.read_record(task_id, ref)['payload']
        _publish(runtime, task_id, ref, payload)
    return True


def _summary(items):
    public = [{key: value for key, value in item.items() if key != 'candidate'} for item in items]
    states = {item['state'] for item in public}
    state = ('skipped' if not states or states == {'skipped'} else 'ready' if states == {'ready'}
             else 'pending' if states == {'pending'} else 'partial')
    return {'state': state, 'items': public}


def finish_report(runtime, receipt, candidates=()):
    if receipt.get('memory_enqueue_failure'):
        return copy.deepcopy(receipt['memory_enqueue_failure'])
    task_id, report_ref = receipt['task_id'], receipt['report_ref']
    try:
        task = runtime.store.get_task(task_id)
        refs = []
        for ref in task.get('memory_job_refs', []):
            job = runtime.store.read_record(task_id, ref)
            if job['payload']['report_ref'] == report_ref:
                refs.append(ref)
                retry = any(item.get('next_action') == 'resubmit' and index < len(candidates)
                            for index, item in enumerate(job['payload']['items']))
                if retry:
                    with runtime.store.transaction():
                        task = runtime.store.get_task(task_id)
                        payload = runtime.store.read_record(task_id, ref)['payload']
                        for index, item in enumerate(payload['items']):
                            if item.get('next_action') == 'resubmit' and index < len(candidates):
                                payload['items'][index] = _candidate(runtime, task, candidates[index])
                        _publish(runtime, task_id, ref, payload)
                if job['record_kind'] == 'memory_pending' or retry:
                    _run(runtime, task_id, ref)
        items = [copy.deepcopy(item) for ref in refs
                 for item in runtime.store.read_record(task_id, ref)['payload']['items']]
        for item in items:
            if item['state'] == 'ready':
                try:
                    runtime.memory.get(task['profile_id'], item['memory_ref'])
                except StoreError as error:
                    item.update(state='skipped', error_code=error.code)
        return _summary(items)
    except (StoreError, OSError, sqlite3.Error) as error:
        code = error.code if isinstance(error, StoreError) else 'STORE_UNAVAILABLE'
        state = 'pending' if code in _RETRY else 'skipped'
        return {'state': state, 'items': [{'state': state, 'error_code': code}]}


def retry_profile(runtime, profile_id):
    if not profile_id:
        return
    with runtime.store.transaction():
        now = datetime.now(UTC).isoformat()
        runtime.store._db.execute("""UPDATE records SET kind='memory_done' WHERE kind='memory_pending'
            AND expires_at IS NOT NULL AND expires_at<=?
            AND task_id IN (SELECT task_id FROM tasks WHERE profile_id=?)""", (now, profile_id))
        pending = runtime.store._db.execute("""SELECT r.task_id,r.record_ref FROM records r JOIN tasks t USING(task_id)
            WHERE t.profile_id=? AND r.kind='memory_pending' AND r.blocked=0
            ORDER BY r.rowid LIMIT 5""", (profile_id,)).fetchall()
    for row in pending:
        try:
            if not _run(runtime, row['task_id'], row['record_ref']):
                break
        except (StoreError, OSError, sqlite3.Error):
            break
