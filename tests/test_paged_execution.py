"""The host follows MCP pages, writes files and submits its evidenced conclusion."""

import csv
import io
import json
import sys
from contextlib import closing
from pathlib import Path

import pytest

from daeri.engine import Engine
from daeri.resources import ResourceRegistry
from daeri.setup import build_setup_resource
from daeri.storage import Store
from test_execution_flow import MEMORY_NONE, ROOT, approve


JQL = 'project = OPS ORDER BY key ASC'


@pytest.fixture
def jira(tmp_path, request):
    log = tmp_path / 'jira-calls.jsonl'
    script = tmp_path / 'jira_mcp.py'
    script.write_text('''import json
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP("paged-jira-fixture")
@server.tool(name="jira_search")
def search(jql: str, limit: int=50, start_at: int=0) -> str:
    with Path(LOG).open("a") as stream:
        stream.write(json.dumps({"jql":jql,"limit":limit,"start_at":start_at})+"\\n")
    issues = [{"key":"OPS-"+str(index),"summary":"Issue "+str(index)}
              for index in range(start_at+1, min(start_at+limit,125)+1)]
    return json.dumps({"issues":issues,"start_at":start_at,"max_results":limit,"total":125})
server.run()
'''.replace('Path(LOG)', 'Path(' + repr(str(log)) + ')'), encoding='utf-8')
    store = Store(tmp_path / 'data')
    item = build_setup_resource(store, 'issue-jira', 'service', 'issue',
        {'server': 'https://jira.example.test', 'auth_type': 'api_token', 'username': 'reader@example.test'})
    item['connection'].update(command=sys.executable, args=['-I', str(script)])
    item['capabilities'] = [cap for cap in item['capabilities'] if cap['capability_id'] == 'search-issues']
    item['policy'] = {'allowed_model_fields': [ROOT], 'export_fields': [ROOT],
                      'retained_fields': [ROOT], 'masking': [], 'memory': MEMORY_NONE}
    if getattr(request, 'param', False):
        item['policy']['memory'] = {'categories': ['procedure'], 'source_fields': [ROOT],
            'context_sources': [], 'retention': 'long_term', 'allow_model_recall': True}
    profile = store.put_profile('service', resources=[item], embedding={'provider': 'keyword_only'})
    ResourceRegistry(store, lambda reference: 'fixture-token').diagnose(profile, 'issue')
    store.close()
    engine = Engine(tmp_path / 'data')
    engine.registry.resolve = lambda reference: 'fixture-token'
    yield engine, profile, log
    engine.close()


def begin(jira, *, max_calls=3):
    engine, profile, _ = jira
    task = engine.call('daeri_task_create', {'request_id': 'create', 'request_text': 'List all OPS issues',
        'context': {'profile_id': profile['profile_id']}})['data']
    search = engine.call('daeri_memory_search', {'task_id': task['task_id'], 'query': 'OPS issues'})
    descriptor = engine.call('daeri_capability_get', {'task_id': task['task_id'], 'instance_id': 'issue',
        'capability_id': 'search-issues'})['data']['descriptor']
    step = {'step_id': 'issues', 'description': 'Read approved issue pages', 'purpose': 'collect',
        'depends_on': [], 'instance_id': 'issue', 'capability_id': 'search-issues',
        'contract_ref': descriptor['contract_ref'], 'scope': {'equals': {'jql': JQL},
            'max_values': {'limit': 50, 'start_at': 100}}, 'input_bindings': []}
    draft = {'purpose': 'List all current OPS issues', 'steps': [step],
        'budget': {'max_tool_calls': max_calls, 'max_external_requests': None, 'max_runtime_ms': 30000, 'resource_limits': []},
        'output': {'format': 'text', 'destination': 'current conversation',
            'resource_rules': [{'instance_id': 'issue', **profile['resources'][0]['policy']}],
            'table_rules': [], 'artifact_rules': []},
        'memory_search_refs': [search['data']['memory_search_ref']], 'memory_refs': [], 'memory_limitations': [],
        'completion_criteria': [{'criterion_id': 'all', 'description': 'All issues collected', 'required': True,
            'step_ids': ['issues']}]}
    saved = engine.call('daeri_plan_submit', {'request_id': 'plan', 'task_id': task['task_id'],
        'expected_task_revision': task['task_revision'], 'draft': draft})
    assert saved['status'] == 'ok', saved
    return saved['data'], step


def page_args(saved, step, approved, offset, *, jql=JQL):
    return {'task_id': saved['task_id'], 'expected_task_revision': approved['task_revision'],
        'plan_id': saved['plan_id'], 'plan_version': saved['plan_version'], 'step_id': step['step_id'],
        'call_id': 'page-' + str(offset), 'approval_id': approved['approval_id'], 'instance_id': 'issue',
        'capability_id': 'search-issues', 'contract_ref': step['contract_ref'],
        'parameters': {'jql': jql, 'limit': 50, 'start_at': offset}}


def read_pages(jira, saved, step, approved, offsets):
    engine = jira[0]
    results, requests = [], []
    for offset in offsets:
        args = page_args(saved, step, approved, offset)
        result = engine.call('daeri_capability_execute', args)
        assert result['status'] in {'ok', 'partial'}, result
        results.append(result['data'])
        requests.append(args)
        approved = {**approved, 'task_revision': result['data']['task_revision']}
    return results, requests


def report(results):
    refs = [result['result_ref'] for result in results]
    return {'summary': 'Collected and checked the requested OPS issues', 'proposed_task_state': 'succeeded',
        'findings': [{'kind': 'observation', 'text': 'Collected current issues', 'evidence_refs': refs}],
        'checks': [{'criterion_id': 'all', 'status': 'met', 'method': 'Host checked collected issue pages', 'evidence_refs': refs}],
        'limitations': [], 'uncertain_effects': [], 'next_actions': [], 'artifacts': []}


def submit(saved, results):
    return {'request_id': 'report', 'task_id': saved['task_id'],
        'expected_task_revision': results[-1]['task_revision'], 'plan_id': saved['plan_id'],
        'plan_version': saved['plan_version'], 'report': report(results)}


def test_three_real_jira_pages_make_complete_report_csv_and_persisted_replay(jira):
    engine, _, log = jira
    saved, step = begin(jira)
    approved = approve(engine, saved)
    pages, requests = read_pages(jira, saved, step, approved, [0, 50, 100])
    args = submit(saved, pages)
    output = Path(engine.store.artifact_workspace(saved['task_id'])) / 'issues.csv'
    with output.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['Issue'])
        for page in pages:
            writer.writerows([[item['key']] for item in page['inline_result']['data']['issues']])
    args['report']['artifacts'] = [{'artifact_id': 'issues', 'label': '이슈 목록', 'local_path': str(output),
        'media_type': 'text/csv', 'source_refs': [page['result_ref'] for page in pages]}]
    stored = engine.call('daeri_result_submit', args)
    assert stored['status'] == 'ok', stored
    assert stored['data']['task_state'] == 'succeeded'
    assert stored['data']['validation']['completion'] == 'met'
    with closing(Engine(engine.store.data_dir)) as reloaded:
        assert reloaded.call('daeri_result_submit', args)['data'] == stored['data']
        for original, request in zip(pages, requests):
            replay = reloaded.call('daeri_capability_execute', request)
            assert replay['data']['result_ref'] == original['result_ref'], replay
        retrieved = reloaded.call('daeri_result_get', {'task_id': saved['task_id'],
            'record_ref': stored['data']['report_ref']})
        assert retrieved['status'] == 'ok', retrieved
        artifact = reloaded.call('daeri_result_get', {'task_id': saved['task_id'],
            'record_ref': stored['data']['artifact_refs'][0]})
        assert artifact['status'] == 'ok', artifact
        contents = Path(artifact['data']['managed_file']['local_path']).read_text()
    assert list(csv.reader(io.StringIO(contents))) == [['Issue']] + [['OPS-' + str(i)] for i in range(1, 126)]
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        {'jql': JQL, 'limit': 50, 'start_at': offset} for offset in [0, 50, 100]]


def test_host_can_report_missing_pages_as_partial(jira):
    engine = jira[0]
    saved, step = begin(jira)
    pages, _ = read_pages(jira, saved, step, approve(engine, saved), [0, 100])
    args = submit(saved, pages)
    args['report']['proposed_task_state'] = 'partial'
    args['report']['checks'][0].update(status='unknown', reason='중간 페이지를 확인하지 못했다.')
    args['report']['limitations'] = ['중간 페이지 미조회']
    stored = engine.call('daeri_result_submit', args)
    assert stored['status'] == 'ok', stored
    assert stored['data']['task_state'] == 'partial'


@pytest.mark.parametrize('jira', [True], indirect=True)
def test_complete_paged_report_automatically_stores_host_curated_knowledge(jira):
    engine, profile, _ = jira
    saved, step = begin(jira)
    pages, _ = read_pages(jira, saved, step, approve(engine, saved), [0, 50, 100])
    args = submit(saved, pages)
    args['memory_candidates'] = [{'draft': {'category': 'procedure',
        'statement': 'OPS 이슈는 응답의 total과 start_at을 확인하며 다음 페이지를 조회한다.',
        'applicability': '같은 Jira 연결의 이슈 검색', 'verification_method': '실제 세 페이지의 수량과 키 대조',
        'instance_ids': ['issue']}, 'source_refs': [page['result_ref'] for page in pages]}]
    stored = engine.call('daeri_result_submit', args)
    assert stored['status'] == 'ok', stored
    assert stored['data']['memory']['state'] == 'ready', stored
    item = engine.memory.get(profile['profile_id'], stored['data']['memory']['items'][0]['memory_ref'])
    assert item['category'] == 'procedure'
    assert {source['record_ref'] for source in item['sources']} == {page['result_ref'] for page in pages}


@pytest.mark.parametrize('jira', [True], indirect=True)
def test_report_without_a_memory_candidate_skips_automatic_memory(jira):
    engine = jira[0]
    saved, step = begin(jira)
    pages, _ = read_pages(jira, saved, step, approve(engine, saved), [0, 100])
    args = submit(saved, pages)
    args['report']['proposed_task_state'] = 'partial'
    args['report']['checks'][0].update(status='unknown', reason='중간 페이지를 확인하지 못했다.')
    stored = engine.call('daeri_result_submit', args)
    assert stored['status'] == 'ok', stored
    assert stored['data']['memory']['state'] == 'skipped'


def test_page_calls_obey_approval_exact_jql_and_shared_budget(jira):
    engine, _, log = jira
    saved, step = begin(jira, max_calls=1)
    unapproved = page_args(saved, step, {'task_revision': saved['task_revision'], 'approval_id': 'invented'}, 0)
    blocked = engine.call('daeri_capability_execute', unapproved)
    assert blocked['status'] == 'error', blocked
    assert blocked['error']['code'] in {'APPROVAL_REQUIRED', 'TASK_STATE_INVALID'}
    assert not log.exists()
    approved = approve(engine, saved)
    changed = page_args(saved, step, approved, 0, jql='project = OTHER ORDER BY key ASC')
    assert engine.call('daeri_capability_execute', changed)['error']['code'] == 'SCOPE_VIOLATION'
    assert not log.exists()
    pages, _ = read_pages(jira, saved, step, approved, [0])
    next_args = page_args(saved, step, {**approved, 'task_revision': pages[-1]['task_revision']}, 50)
    assert engine.call('daeri_capability_execute', next_args)['error']['code'] == 'BUDGET_EXCEEDED'
    assert len(log.read_text().splitlines()) == 1
