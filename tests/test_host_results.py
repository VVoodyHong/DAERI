from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from daeri.application.results import ResultService
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


@pytest.fixture
def host_result(tmp_path):
    store = Store(tmp_path)
    profile = store.put_profile('host-results')
    task = {'task_id': 'task-host-results', 'profile_id': profile['profile_id'],
            'task_revision': 1, 'state': 'running', 'created_at': datetime.now(UTC).isoformat(),
            'profile_snapshot': profile}
    store.insert_task(task)
    payload = {'result': {'rows': [{'value': 5}]}, 'step_id': 'read', 'instance_id': 'db',
               'capability_id': 'query', 'contract_ref': 'read-v2', 'scope': {},
               'source_complete': False, 'plan_id': 'plan', 'plan_version': 1}
    reference = store.write_record(task['task_id'], 'call_result', payload)
    record = {**payload, 'payload': payload, 'record_kind': 'call_result', 'expires_at': None,
              'provided_paths': [('rows', 0, 'value')], 'allowed_paths': [('rows', 0, 'value')]}
    def load(ref, mode='model'):
        assert ref == reference
        store.read_record(task['task_id'], ref)
        return record
    plan = {'plan_id': 'plan', 'plan_version': 1, 'draft': {
        'steps': [{'step_id': 'read', 'instance_id': 'db', 'capability_id': 'query',
                   'contract_ref': 'read-v2', 'scope': {}}],
        'completion_criteria': [{'criterion_id': 'sample', 'description': '요청한 표본 확인',
                                 'required': True, 'step_ids': ['read']}],
        'memory_refs': [], 'output': {'format': '사용자 요청 형식', 'destination': '현재 대화',
                                     'resource_rules': []}}}
    report = {'report': '요청한 표본 값은 5다. 전체 데이터의 전수 조사는 아니다.',
              'state': 'succeeded', 'evidence_refs': [reference], 'artifacts': []}
    yield ResultService(store), task, plan, report, reference, record, load
    store.close()


def test_host_can_complete_an_approved_sample_without_claiming_a_full_dataset(host_result):
    service, task, plan, report, _, _, load = host_result
    saved = submit(service, task, plan, report, load_evidence=load)
    assert saved['task_state'] == 'succeeded'
    assert saved['validation']['completion'] == 'met'
    assert service.store.read_record(task['task_id'], saved['report_ref'])['payload']['content']


def submit(service, task, plan, report, *, load_evidence):
    return service.submit(task, plan, **report, load_evidence=load_evidence)


def test_host_decides_completion_without_a_report_checklist(host_result):
    service, task, plan, report, _, _, load = host_result
    plan['draft']['completion_criteria'][0]['step_ids'] = ['host-analysis']
    saved = submit(service, task, plan, report, load_evidence=load)
    assert saved['task_state'] == 'succeeded'
    assert saved['validation']['completion_assessed_by'] == 'host'


def test_host_report_can_cite_current_memory(host_result):
    service, task, plan, report, reference, _, load = host_result
    memory_ref = 'memory-found-during-work'
    report['evidence_refs'].append(memory_ref)

    def sources(ref, mode='model'):
        if ref == memory_ref:
            return {'record_kind': 'memory', 'payload': {'statement': '검증된 조회 절차'}, 'expires_at': None}
        return load(ref, mode)

    saved = submit(service, task, plan, report, load_evidence=sources)
    assert saved['task_state'] == 'succeeded'
    stored = service.store.read_record(task['task_id'], saved['report_ref'])['payload']
    assert set(stored['evidence_refs']) == {reference, memory_ref}


@pytest.mark.parametrize('extension,media_type', [('csv', 'text/csv'), ('xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'), ('pdf', 'application/pdf')])
def test_host_file_is_preserved_without_a_format_generator(host_result, extension, media_type):
    service, task, plan, report, ref, _, load = host_result
    workspace = Path(service.store.artifact_workspace(task['task_id']))
    path = workspace / ('answer.' + extension)
    content = b'host-created output\x00\xff'
    path.write_bytes(content)
    report['artifacts'] = [{'artifact_id': 'answer', 'label': '결과', 'local_path': str(path),
                            'media_type': media_type, 'source_refs': [ref]}]
    saved = submit(service, task, plan, report, load_evidence=load)
    file = service.store.artifact_file(task['task_id'], saved['artifact_refs'][0])
    assert Path(file['local_path']).read_bytes() == content
    assert file['media_type'] == media_type
    assert path.read_bytes() == content


def test_host_file_requires_export_permission_for_its_provided_source(host_result):
    service, task, plan, report, ref, record, load = host_result
    path = Path(service.store.artifact_workspace(task['task_id'])) / 'answer.txt'
    path.write_text('5')
    record['allowed_paths'] = []
    report['artifacts'] = [{'artifact_id': 'answer', 'label': '결과', 'local_path': str(path),
                            'media_type': 'text/plain', 'source_refs': [ref]}]
    with pytest.raises(StoreError, match='RESULT_VALIDATION_FAILED'):
        submit(service, task, plan, report, load_evidence=load)
    assert not service.store._db.execute('SELECT * FROM artifact_files').fetchall()


def test_host_file_checks_only_its_declared_sources(host_result):
    service, task, plan, report, reference, record, load = host_result
    file_report(host_result)
    unrelated = service.store.write_record(task['task_id'], 'call_result', {'result': {'private': 'model-only'}})
    task['calls'] = [{'result_ref': unrelated}]
    report['evidence_refs'].append(unrelated)

    def sources(ref, mode='model'):
        if ref == unrelated:
            return {'record_kind': 'call_result', 'result': {'private': 'model-only'}, 'expires_at': None,
                    'provided_paths': ['/private'], 'allowed_paths': [] if mode == 'export' else ['/private']}
        return load(ref, mode)

    saved = submit(service, task, plan, report, load_evidence=sources)
    assert task['artifact_ids']['answer']['source_refs'] == [reference]
    assert Path(service.store.artifact_file(task['task_id'], saved['artifact_refs'][0])['local_path']).read_bytes() == b'5'


@pytest.mark.parametrize('short_source', ['finding', 'artifact'])
def test_each_file_uses_only_its_own_contributors_for_expiry(host_result, short_source):
    service, task, plan, report, reference, _, load = host_result
    deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    short_ref = service.store.write_record(task['task_id'], 'call_result', {'result': {'other': 1}}, deadline)
    if short_source == 'finding':
        report['evidence_refs'].append(short_ref)
    else:
        path = Path(service.store.artifact_workspace(task['task_id'])) / 'short.txt'
        path.write_text('1')
        report['artifacts'] = [{'artifact_id': 'short', 'label': '별도 결과', 'local_path': str(path),
            'media_type': 'text/plain', 'source_refs': [short_ref]}]
    path = Path(service.store.artifact_workspace(task['task_id'])) / 'answer.txt'
    path.write_text('5')
    report['artifacts'].append({'artifact_id': 'answer', 'label': '결과', 'local_path': str(path),
        'media_type': 'text/plain', 'source_refs': [reference]})

    def sources(ref, mode='model'):
        if ref == short_ref:
            return {'record_kind': 'call_result', 'result': {'other': 1}, 'expires_at': deadline,
                    'provided_paths': ['/other'], 'allowed_paths': ['/other']}
        return load(ref, mode)

    saved = submit(service, task, plan, report, load_evidence=sources)
    assert service.store.read_record(task['task_id'], saved['report_ref'])['expires_at'] == deadline
    answer = service.store.artifact_file(task['task_id'], saved['artifact_refs'][-1])
    assert answer['expires_at'] is None


def test_host_file_inherits_the_shorter_expiry_of_an_intermediate_report(host_result):
    service, task, plan, report, ref, _, load = host_result
    deadline = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    intermediate = service.store.write_record(task['task_id'], 'report', {'evidence_refs': [ref]}, deadline)
    file_report(host_result)
    report['artifacts'][0]['source_refs'] = [intermediate]

    def sources(reference, mode='model'):
        if reference == intermediate:
            return {**service.store.read_record(task['task_id'], reference), 'record_kind': 'report'}
        return load(reference, mode)

    saved = submit(service, task, plan, report, load_evidence=sources)
    file = service.store.artifact_file(task['task_id'], saved['artifact_refs'][0])
    assert file['expires_at'] == deadline
    assert service.store.read_record(task['task_id'], saved['report_ref'])['expires_at'] == deadline
    assert task['artifact_ids']['answer']['source_refs'] == [ref]


@pytest.mark.parametrize('source_count', [254, 255, 300])
def test_host_file_export_preserves_wide_source_graphs(host_result, source_count):
    service, task, plan, report, ref, _, load = host_result
    with service.store.transaction():
        sources = [service.store.write_record(task['task_id'], 'local_check', {'value': index})
                   for index in range(source_count)]
        root = service.store.write_record(task['task_id'], 'report', {'evidence_refs': [*sources, ref]})
    file_report(host_result)
    report['artifacts'][0]['source_refs'] = [root]

    def graph(reference, mode='model'):
        if reference == ref:
            return load(reference, mode)
        record = service.store.read_record(task['task_id'], reference)
        return {**record, 'provided_paths': ['/value'], 'allowed_paths': ['/value']}

    saved = submit(service, task, plan, report, load_evidence=graph)
    assert saved['artifact_refs']
    assert set(task['artifact_ids']['answer']['source_refs']) == {*sources, ref}


@pytest.mark.parametrize('intermediate_count,accepted', [(30, True), (31, False)])
def test_host_file_export_checks_shared_sources_at_the_deeper_path(host_result, intermediate_count, accepted):
    service, task, plan, report, ref, _, load = host_result
    records = {'aa-shared': {'record_kind': 'report', 'expires_at': None,
                            'payload': {'evidence_refs': [ref]}}}
    root = 'aa-shared'
    for index in range(intermediate_count):
        current = f'zz-report-{index}'
        records[current] = {'record_kind': 'report', 'expires_at': None,
                            'payload': {'evidence_refs': [root]}}
        root = current
    file_report(host_result)
    report['artifacts'][0]['source_refs'] = ['aa-shared', root]

    def graph(reference, mode='model'):
        return load(reference, mode) if reference == ref else records[reference]

    if accepted:
        assert submit(service, task, plan, report, load_evidence=graph)['artifact_refs']
    else:
        with pytest.raises(StoreError, match='RESULT_VALIDATION_FAILED'):
            submit(service, task, plan, report, load_evidence=graph)


def test_importing_a_file_outside_the_task_workspace_is_rejected(host_result, tmp_path):
    service, task, plan, report, ref, _, load = host_result
    path = tmp_path / 'unrelated.txt'
    path.write_text('unrelated')
    report['artifacts'] = [{'artifact_id': 'answer', 'label': '결과', 'local_path': str(path),
                            'media_type': 'text/plain', 'source_refs': [ref]}]
    with pytest.raises(StoreError):
        submit(service, task, plan, report, load_evidence=load)
    assert not service.store._db.execute('SELECT * FROM artifact_files').fetchall()


def test_file_workspace_symlinks_are_rejected(host_result, tmp_path):
    service, task, _, _, _, _, _ = host_result
    workspace = Path(service.store.artifact_workspace(task['task_id']))
    external = tmp_path / 'elsewhere'
    external.mkdir()
    workspace.rmdir()
    workspace.symlink_to(external, target_is_directory=True)
    with pytest.raises(StoreError):
        service.store.artifact_workspace(task['task_id'])


def file_report(host_result, name='answer.txt', content=b'5'):
    service, task, _, report, ref, _, _ = host_result
    path = Path(service.store.artifact_workspace(task['task_id'])) / name
    path.write_bytes(content)
    report['artifacts'] = [{'artifact_id': 'answer', 'label': '결과', 'local_path': str(path),
                            'media_type': 'application/octet-stream', 'source_refs': [ref]}]
    return path


def test_large_host_file_is_streamed_without_the_old_generator_size_limit(host_result):
    service, task, plan, report, _, _, load = host_result
    file_report(host_result, 'answer.bin', b'x' * (11 * 1024 * 1024))
    saved = submit(service, task, plan, report, load_evidence=load)
    file = service.store.artifact_file(task['task_id'], saved['artifact_refs'][0])
    assert file['size_bytes'] == 11 * 1024 * 1024


def test_failed_report_preserves_workspace_input_and_publishes_no_file(host_result, monkeypatch):
    service, task, plan, report, _, _, load = host_result
    source = file_report(host_result)
    original = service.store.write_record
    def fail_report(task_id, kind, *args, **kwargs):
        if kind == 'report':
            raise StoreError('STORE_UNAVAILABLE', 'fixture storage failure')
        return original(task_id, kind, *args, **kwargs)
    monkeypatch.setattr(service.store, 'write_record', fail_report)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
        submit(service, task, plan, report, load_evidence=load)
    assert source.read_bytes() == b'5'
    assert not service.store._db.execute('SELECT * FROM artifact_files').fetchall()
    service.store.cleanup()
    assert not list((service.store.data_dir / 'artifacts').iterdir())


def test_committed_file_cleans_only_its_unchanged_workspace_copy(host_result):
    service, task, plan, report, _, _, load = host_result
    source = file_report(host_result)
    saved = submit(service, task, plan, report, load_evidence=load)
    service.store.cleanup()
    assert not source.exists()
    file = service.store.artifact_file(task['task_id'], saved['artifact_refs'][0])
    assert Path(file['local_path']).read_bytes() == b'5'


def test_cleanup_preserves_workspace_changes_after_a_file_was_registered(host_result):
    service, task, plan, report, _, _, load = host_result
    source = file_report(host_result)
    saved = submit(service, task, plan, report, load_evidence=load)
    source.write_bytes(b'new unfinished work')
    service.store.cleanup()
    assert source.read_bytes() == b'new unfinished work'
    file = service.store.artifact_file(task['task_id'], saved['artifact_refs'][0])
    assert Path(file['local_path']).read_bytes() == b'5'


def test_same_file_id_cannot_replace_registered_bytes(host_result):
    service, task, plan, report, _, _, load = host_result
    source = file_report(host_result)
    saved = submit(service, task, plan, report, load_evidence=load)
    source.write_bytes(b'changed')
    with pytest.raises(StoreError, match='RESULT_VALIDATION_FAILED'):
        submit(service, task, plan, report, load_evidence=load)
    file = service.store.artifact_file(task['task_id'], saved['artifact_refs'][0])
    assert Path(file['local_path']).read_bytes() == b'5'


def test_source_symlink_cannot_import_an_unrelated_file(host_result, tmp_path):
    service, task, plan, report, _, _, load = host_result
    source = file_report(host_result)
    source.unlink()
    secret = tmp_path / 'secret.txt'
    secret.write_text('unrelated')
    source.symlink_to(secret)
    with pytest.raises(StoreError):
        submit(service, task, plan, report, load_evidence=load)
    assert secret.read_text() == 'unrelated'


def test_profile_deletion_removes_its_host_file_workspace(host_result):
    from daeri.config.profile_deletion import preview, delete
    service, task, _, _, _, _, _ = host_result
    workspace = Path(service.store.artifact_workspace(task['task_id']))
    (workspace / 'unfinished.txt').write_text('unfinished')
    class NoCredentials:
        def delete_if_present(self, reference):
            raise AssertionError('This fixture has no credentials')
    pending = preview(service.store, task['profile_id'])
    delete(service.store, task['profile_id'], pending['deletion_token'], NoCredentials())
    assert not workspace.exists()
