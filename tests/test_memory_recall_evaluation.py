import json
import socket

import httpx
import pytest

from evaluations import memory_recall as evaluation
from daeri.embedding import OpenAICompatibleEmbedder
from daeri.memory import MemoryService


def test_scores_use_top_k_and_positive_gold_without_duplicate_credit():
    result = evaluation.score_case(['a', 'b'], ['noise', 'a', 'a', 'b'], 3)
    assert result == {'recall_at_k': 0.5, 'reciprocal_rank': 0.5,
                      'expected_ranks': {'a': 2, 'b': None}}
    assert evaluation.score_case([], ['noise'], 5) == {
        'recall_at_k': None, 'reciprocal_rank': None, 'expected_ranks': {}}
    assert evaluation.score_case(['a'], [], 5)['reciprocal_rank'] == 0


def test_fixed_corpus_covers_current_evidence_and_access_boundaries():
    corpus = evaluation.load_dataset()
    assert len(corpus['queries']) == 14
    assert {'identifier', 'paraphrase', 'correction', 'multi_evidence',
            'unrelated', 'isolation'} == {case['group'] for case in corpus['queries']}
    memories = {item['id']: item for item in corpus['memories']}
    obsolete = {item['replaces'] for item in memories.values() if 'replaces' in item}
    assert obsolete
    for case in corpus['queries']:
        for reference in case['expected']:
            item = memories[reference]
            assert item['profile'] == 'primary' and item['access'] == 'allowed'
            assert reference not in obsolete


def test_keyword_evaluation_is_repeatable_and_uses_no_network_or_user_store(monkeypatch, tmp_path):
    def no_network(*args, **kwargs):
        pytest.fail('Keyword evaluation attempted network access')
    monkeypatch.setattr(socket, 'getaddrinfo', no_network)
    monkeypatch.setenv('DAERI_DATA_DIR', str(tmp_path / 'untouched-user-store'))
    first = evaluation.evaluate(evaluation.load_dataset(), {'provider': 'keyword_only'})
    second = evaluation.evaluate(evaluation.load_dataset(), {'provider': 'keyword_only'})
    assert first['status'] == 'complete' and first['integrity_passed']
    assert first['evaluation_kind'] == 'keyword_baseline'
    assert first['model_id'] is None
    assert first['metrics']['positive_queries'] == 10
    assert first['metrics']['no_gold_queries'] == 4
    assert first['metrics']['forbidden_results'] == 0
    assert first['metrics']['search_ms']['p95'] >= 0
    assert first['dataset_sha256'] == second['dataset_sha256']
    assert [(case['id'], case['retrieved']) for case in first['cases']] == [
        (case['id'], case['retrieved']) for case in second['cases']]
    assert all(case['expected_ranks'] and case['recall_at_k'] == 1
               for case in first['cases'] if case['group'] in ('identifier', 'correction'))
    assert not (tmp_path / 'untouched-user-store').exists()


def test_an_inaccessible_result_fails_evaluation(monkeypatch):
    original = MemoryService.search

    def leaking_search(self, profile_id, *args, **kwargs):
        result = original(self, profile_id, *args, **kwargs)
        foreign = self.store._db.execute(
            'SELECT memory_ref FROM memory_versions WHERE profile_id!=?', (profile_id,)).fetchone()[0]
        result['items'] = [{'memory_ref': foreign}]
        return result
    monkeypatch.setattr(MemoryService, 'search', leaking_search)
    result = evaluation.evaluate(evaluation.load_dataset(), {'provider': 'keyword_only'})
    assert not result['integrity_passed']
    assert result['metrics']['forbidden_results'] == 14


def test_local_fixture_checks_the_hybrid_evaluation_path(monkeypatch, tmp_path):
    class NumericalFixture:
        model_id, dimensions = 'numeric-test-fixture', 3

        def embed_document(self, text):
            return [1.0, 0.5, 0.1]

        def embed_query(self, text):
            return [1.0, 0.5, 0.1]

    monkeypatch.setattr(evaluation.LocalEmbedder, 'load', lambda path: NumericalFixture())
    result = evaluation.evaluate(evaluation.load_dataset(), {'provider': 'local'}, model_cache=tmp_path)
    assert result['status'] == 'complete' and result['integrity_passed']
    assert result['model_id'] == 'numeric-test-fixture'
    assert all(case['retrieval_state'] == 'hybrid' for case in result['cases'])
    assert all(len(case['retrieved']) == 5 for case in result['cases'] if not case['expected'])
    assert result['groups']['unrelated']['recall_at_k'] is None


@pytest.mark.parametrize('gold', ['billing_old', 'foreign_sentinel', 'revoked_sentinel', 'unknown'])
def test_invalid_gold_is_rejected_before_loading_a_model(monkeypatch, gold, tmp_path):
    dataset = evaluation.load_dataset()
    dataset['queries'][0]['expected'] = [gold]
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid gold reached model loading')
    monkeypatch.setattr(evaluation.LocalEmbedder, 'load', forbidden)
    with pytest.raises(ValueError):
        evaluation.evaluate(dataset, {'provider': 'local'}, model_cache=tmp_path)


@pytest.mark.parametrize('breakage', ['index', 'query'])
def test_api_failures_cannot_be_presented_as_semantic_quality(monkeypatch, breakage):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if breakage == 'index' or len(calls) > len(evaluation.load_dataset()['memories']):
            return httpx.Response(503, json={'detail': 'private endpoint or token'})
        return httpx.Response(200, json={'model': 'fixture', 'data': [
            {'index': 0, 'embedding': [1.0, 0.5, 0.1]}]})

    class Credential:
        def get(self, reference):
            return 'test-only-token'

    config = {'provider': 'api', 'adapter': 'openai_compatible',
              'endpoint': 'https://embedding.example.test/v1/embeddings',
              'model': 'fixture', 'credential_ref': 'env:TEST_EMBEDDING_TOKEN'}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr('daeri.memory.create_api_embedder', lambda config, **kwargs:
            OpenAICompatibleEmbedder(config, credentials=Credential(), client=client))
        result = evaluation.evaluate(evaluation.load_dataset(), config)
    assert result['status'] == 'incomplete'
    assert result['metrics']['recall_at_k'] is None
    assert result['metrics']['mrr_at_k'] is None
    assert calls
    assert 'test-only-token' not in json.dumps(result)
    assert config['endpoint'] not in json.dumps(result)


def test_cli_without_local_model_reports_unavailable_without_scores(tmp_path, capsys):
    assert evaluation.main(['--provider', 'local', '--model-cache', str(tmp_path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'unavailable'
    assert 'metrics' not in result


def test_cli_writes_report_and_sets_failure_exit_for_integrity(monkeypatch, tmp_path, capsys):
    destination = tmp_path / 'evaluation.json'
    assert evaluation.main(['--output', str(destination)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert json.loads(destination.read_text()) == report
    monkeypatch.setattr(evaluation, 'evaluate', lambda *args, **kwargs:
                        {'status': 'complete', 'integrity_passed': False})
    assert evaluation.main([]) == 1
    capsys.readouterr()


def test_cli_rejects_secret_config_before_any_transmission(monkeypatch, tmp_path, capsys):
    config = tmp_path / 'embedding.yaml'
    config.write_text('embedding:\n  provider: api\n  api_key: private-key\n')
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid configuration reached evaluation')
    monkeypatch.setattr(evaluation, 'evaluate', forbidden)
    assert evaluation.main(['--provider', 'api', '--embedding-config', str(config)]) == 1
    result = capsys.readouterr().out
    assert json.loads(result)['status'] == 'unavailable'
    assert 'private-key' not in result
