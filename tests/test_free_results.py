"""Host responses are preserved without a report template or completion checklist."""

from contextlib import closing

import pytest

from daeri.application.runtime import Runtime
from daeri.config.setup import build_setup_resource


@pytest.fixture
def approved_host_task(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'app.py').write_text('print(42)\n')
    with closing(Runtime(tmp_path / 'data')) as runtime:
        resource = build_setup_resource(runtime.store, 'source-local', 'service', 'source', {'path': str(root)})
        profile = runtime.store.put_profile('service', resources=[resource], embedding={'provider': 'keyword_only'})
        runtime.registry.diagnose(profile, 'source')
        task = runtime.call('daeri_task_create', {'request_text': 'Explain this project',
                           'context': {'profile_id': profile['profile_id']}})['data']
        planned = runtime.call('daeri_plan_submit', {'task_id': task['task_id'], 'draft': {
            'purpose': 'Inspect and explain the project',
            'steps': [{'step_id': 'inspect', 'instance_id': 'source', 'description': 'Inspect source'}]}})
        assert planned['status'] == 'ok', planned
        approved = runtime.call('daeri_approval_request', {'task_id': task['task_id'],
            'plan_id': planned['data']['plan_id'], 'decision': 'approve', 'user_response': '진행해'})
        assert approved['status'] == 'ok', approved
        yield runtime, task['task_id'], planned['data']['plan_id']


@pytest.mark.parametrize('content', ['# 결과\n\n|항목|값|\n|---|---|\n|출력|42|',
                                   {'body': {'total': 42}, 'tables': [{'rows': [[42]]}]},
                                   ['자유 형식', 42], 42])
def test_response_content_is_preserved_and_real_sources_are_linked(approved_host_task, content):
    runtime, task_id, _ = approved_host_task
    observed = runtime.call('daeri_capability_execute', {'task_id': task_id, 'step_id': 'inspect',
        'capability_id': 'source-observe', 'parameters': {'summary': 'app.py prints 42', 'paths': ['app.py']}})
    assert observed['status'] == 'ok', observed
    submitted = runtime.call('daeri_result_submit', {'task_id': task_id, 'report': content})
    assert submitted['status'] == 'ok', submitted
    assert submitted['data']['task_state'] == 'succeeded'
    payload = runtime.store.read_record(task_id, submitted['data']['report_ref'])['payload']
    assert payload['content'] == content
    assert observed['data']['result_ref'] in payload['evidence_refs']
    assert payload['validation']['completion_assessed_by'] == 'host'


def test_host_can_report_reasoning_without_inventing_a_resource_call(approved_host_task):
    runtime, task_id, _ = approved_host_task
    submitted = runtime.call('daeri_result_submit', {'task_id': task_id,
        'report': '사용자 요청과 현재 맥락을 종합한 답변입니다.'})
    assert submitted['status'] == 'ok', submitted
    task = runtime.store.get_task(task_id)
    assert task['state'] == 'succeeded' and task['calls'] == []
    payload = runtime.store.read_record(task_id, submitted['data']['report_ref'])['payload']
    assert payload['evidence_refs'] == [task['request_ref']]


def test_explicit_unknown_evidence_is_rejected_without_finishing_task(approved_host_task):
    runtime, task_id, _ = approved_host_task
    submitted = runtime.call('daeri_result_submit', {'task_id': task_id, 'report': '답변',
                                                   'evidence_refs': ['missing-record']})
    assert submitted['status'] == 'error'
    assert runtime.store.get_task(task_id)['state'] == 'running'


def test_selected_status_is_metadata_and_content_keys_remain_user_data(approved_host_task):
    runtime, task_id, _ = approved_host_task
    content = {'proposed_task_state': 'cancelled', 'findings': '자유 내용', 'checks': [1, 2]}
    submitted = runtime.call('daeri_result_submit', {'task_id': task_id, 'state': 'partial', 'report': content})
    assert submitted['status'] == 'ok', submitted
    assert submitted['data']['task_state'] == 'partial'
    assert runtime.store.read_record(task_id, submitted['data']['report_ref'])['payload']['content'] == content


def test_file_sources_default_to_the_report_sources(approved_host_task):
    from pathlib import Path

    runtime, task_id, _ = approved_host_task
    path = Path(runtime.store.artifact_workspace(task_id)) / 'answer.anything'
    path.write_bytes(b'host-created bytes')
    submitted = runtime.call('daeri_result_submit', {'task_id': task_id, 'report': '요청에 대한 결과 파일',
        'artifacts': [{'artifact_id': 'answer', 'label': '결과', 'local_path': str(path),
                       'media_type': 'application/x-host-result'}]})
    assert submitted['status'] == 'ok', submitted
    artifact = runtime.store.artifact_file(task_id, submitted['data']['artifact_refs'][0])
    assert Path(artifact['local_path']).read_bytes() == b'host-created bytes'


def test_many_observations_preserve_report_read_and_replay(approved_host_task):
    runtime, task_id, _ = approved_host_task
    references = []
    for index in range(256):
        observed = runtime.call('daeri_capability_execute', {'task_id': task_id, 'step_id': 'inspect',
            'capability_id': 'source-observe',
            'parameters': {'summary': f'관찰 {index}', 'paths': ['app.py']}})
        assert observed['status'] == 'ok', observed
        references.append(observed['data']['result_ref'])
    args = {'task_id': task_id, 'request_id': 'many-observations', 'report': '관찰 결과를 종합했습니다.'}
    submitted = runtime.call('daeri_result_submit', args)
    assert submitted['status'] == 'ok', submitted
    read = runtime.call('daeri_result_get', {'task_id': task_id,
                                          'record_ref': submitted['data']['report_ref']})
    assert read['status'] == 'ok', read
    assert set(read['data']['evidence_refs']) == set(references)
    replayed = runtime.call('daeri_result_submit', args)
    assert replayed['status'] == 'ok', replayed
    assert replayed['data']['report_ref'] == submitted['data']['report_ref']


def test_free_content_survives_encrypted_backup_and_restore(approved_host_task, tmp_path):
    from daeri.config.profile_portability import backup_profile, inspect_backup, restore_profile
    from daeri.infrastructure.storage import Store

    runtime, task_id, _ = approved_host_task
    content = {'profile_id': 'part-of-the-answer', 'evidence_refs': ['user-data']}
    for _ in range(32):
        content = [content]
    submitted = runtime.call('daeri_result_submit', {'task_id': task_id, 'report': content})
    assert submitted['status'] == 'ok', submitted
    report_ref = submitted['data']['report_ref']
    archive = tmp_path / 'profile.daeri'
    password = 'test-only-free-report-backup'
    backup_profile(runtime, runtime.store.get_task(task_id)['profile_id'], archive, password)
    preview = inspect_backup(archive, password)
    with closing(Store(tmp_path / 'restored')) as destination:
        restore_profile(destination, archive, password, preview['confirmation_token'])
        assert destination.read_record(task_id, report_ref)['payload']['content'] == content
