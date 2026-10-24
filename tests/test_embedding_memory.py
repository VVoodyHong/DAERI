"""Profile-selected embedding, remote permission, and configuration preservation."""

from contextlib import closing
import json

import pytest

from daeri.memory import MemoryService
from daeri.setup import export_profile, import_profile
from daeri.storage import Store, StoreError
from test_memory import (NumericalFixtureEmbedder, candidate, configured_resource,
                         memory_policy, save, source)


def api_config(**changes):
    return {"provider": "api", "adapter": "openai_compatible", "endpoint": "https://embedding.example.test/v1/embeddings",
            "model": "company-embedding", "credential_ref": "credential:company-embedding",
            **changes}


def profile(store, name="alpha", **changes):
    return store.put_profile(name, resources=[configured_resource()], memory_policy=memory_policy(), **changes)


class RecordingAPI(NumericalFixtureEmbedder):
    remote = True
    calls = []

    def __init__(self, config, **kwargs):
        self.model_id = config["model"]

    def embed_document(self, text):
        self.calls.append(("document", self.model_id, text))
        return super().embed_document(text)

    def embed_query(self, text):
        self.calls.append(("query", self.model_id, text))
        return super().embed_query(text)


@pytest.fixture
def api(monkeypatch):
    RecordingAPI.calls = []
    monkeypatch.setattr("daeri.memory.create_api_embedder", RecordingAPI)
    return RecordingAPI


def test_profile_embedding_configuration_round_trips_and_preserves_on_update(tmp_path):
    with closing(Store(tmp_path / "data")) as store:
        original = profile(store, embedding=api_config())
        assert original["embedding"]["provider"] == "api"
        changed = store.put_profile("alpha", resources=original["resources"], memory_policy=memory_policy())
        assert changed["embedding"] == original["embedding"]
        value = export_profile(store, "alpha")
        value["name"] = "copied"
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(value))
        assert import_profile(store, path)["embedding"] == original["embedding"]


def test_api_selection_indexes_and_searches_without_local_model(tmp_path, api, monkeypatch):
    monkeypatch.setattr("daeri.memory.LocalEmbedder.load", lambda *a: pytest.fail("API mode loaded a local model"))
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        item = save(service, selected)
        assert item["index_state"] == "ready"
        result = service.search(selected["profile_id"], "E_CONN_001")
        assert result["retrieval_state"] == "hybrid"
        assert [entry[0] for entry in api.calls] == ["document", "query"]
        assert not (tmp_path / "models").exists()


@pytest.mark.parametrize('pending_index', [False, True])
def test_engine_distinguishes_complete_keyword_search_from_pending_index(tmp_path, pending_index):
    from daeri.engine import Engine
    with closing(Engine(tmp_path)) as engine:
        selected = profile(engine.store, embedding={'provider': 'keyword_only'})
        item = save(engine.memory, selected)
        if pending_index:
            engine.store._db.execute('DELETE FROM memory_fts WHERE memory_ref=?', (item['memory_ref'],))
        task = engine.call('daeri_task_create', {'request_id': 'keyword-task',
            'request_text': 'E_CONN_001 처리 방법을 찾아줘', 'context': {'profile_id': selected['profile_id']}})
        result = engine.call('daeri_memory_search', {'task_id': task['data']['task_id'], 'query': 'E_CONN_001'})
        assert result['data']['retrieval_state'] == 'keyword_only'
        if pending_index:
            assert result['status'] == 'partial'
            assert result['error']['code'] == 'MEMORY_SEARCH_INCOMPLETE'
            assert result['data']['limitations']
        else:
            assert result['status'] == 'ok'
            assert result['error'] is None
            assert result['data']['limitations'] == []
            assert result['data']['items']


def test_engine_returns_complete_empty_keyword_search(tmp_path):
    from daeri.engine import Engine
    with closing(Engine(tmp_path)) as engine:
        selected = profile(engine.store, embedding={'provider': 'keyword_only'})
        task = engine.call('daeri_task_create', {'request_id': 'empty-keyword-task',
            'request_text': '운영 경험을 찾아줘', 'context': {'profile_id': selected['profile_id']}})
        result = engine.call('daeri_memory_search', {'task_id': task['data']['task_id'], 'query': '운영'})
        assert result['status'] == 'ok'
        assert result['error'] is None
        assert result['data']['items'] == []
        assert result['data']['limitations'] == []


@pytest.mark.parametrize("hide_existing", [False, True])
def test_api_empty_eligible_search_is_complete_without_a_provider_request(tmp_path, api, hide_existing):
    from daeri.engine import Engine
    with closing(Engine(tmp_path)) as engine:
        selected = profile(engine.store, embedding=api_config())
        if hide_existing:
            save(engine.memory, selected)
            engine.store.put_profile("alpha", resources=selected["resources"],
                memory_policy=memory_policy(model_allowed=False))
        api.calls.clear()
        task = engine.call("daeri_task_create", {"request_id": "empty-api", "request_text": "운영 경험 조회",
            "context": {"profile_id": selected["profile_id"]}})["data"]
        result = engine.call("daeri_memory_search", {"task_id": task["task_id"], "query": "운영 경험"})
        assert result["status"] == "ok", result
        assert result["error"] is None
        assert result["data"]["items"] == []
        assert result["data"]["retrieval_state"] == "keyword_only"
        assert result["data"]["limitations"] == []
        assert api.calls == []


def test_api_failure_with_eligible_memory_still_reports_partial_search(tmp_path, api, monkeypatch):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        save(service, selected)
        def failed_query(self, text):
            raise StoreError("EMBEDDING_API_UNAVAILABLE", "test provider is unavailable")
        monkeypatch.setattr(api, "embed_query", failed_query)
        result = service.search(selected["profile_id"], "E_CONN_001")
        assert result["items"]
        assert result["retrieval_state"] == "keyword_only"
        assert result["limitations"]


def test_keyword_only_never_calls_api(tmp_path, api):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding={"provider": "keyword_only"})
        service = MemoryService(store)
        save(service, selected)
        result = service.search(selected["profile_id"], "E_CONN_001")
        assert result["items"]
        assert result["retrieval_state"] == "keyword_only"
        assert api.calls == []


def test_memory_permission_is_checked_before_query_leaves_pc(tmp_path, api):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        save(service, selected)
        api.calls.clear()
        store.put_profile("alpha", resources=selected["resources"], memory_policy=memory_policy(model_allowed=False))
        assert service.search(selected["profile_id"], "PRIVATE_QUERY")["items"] == []
        assert api.calls == []
        with pytest.raises(StoreError, match="ACCESS_DENIED"):
            service.search(selected["profile_id"], "PRIVATE_QUERY", instance_ids=["not-allowed"])
        assert api.calls == []


def test_model_change_keeps_memory_but_requires_matching_vectors(tmp_path, api):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        item = save(service, selected)
        profile(store, embedding=api_config(model="another-model"))
        result = service.search(selected["profile_id"], "semantic-only-query")
        assert result["items"] == []
        assert result["limitations"]
        assert service.get(selected["profile_id"], item["memory_ref"])["statement"] == candidate()["statement"]
        assert service.reindex(selected["profile_id"]) == {"ready": 1, "pending": 0}
        assert service.search(selected["profile_id"], "semantic-only-query")["items"]


def test_keyword_profile_does_not_use_other_profile_api(tmp_path, api):
    with closing(Store(tmp_path)) as store:
        remote = profile(store, embedding=api_config())
        local = profile(store, name="beta", embedding={"provider": "keyword_only"})
        service = MemoryService(store)
        save(service, remote)
        save(service, local, sources=[source(service, local, key="beta")])
        api.calls.clear()
        assert service.search(local["profile_id"], "E_CONN_001")["retrieval_state"] == "keyword_only"
        assert api.calls == []


def test_keyword_only_index_is_ready_without_pending_vector_work(tmp_path, api):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding={"provider": "keyword_only"})
        service = MemoryService(store)
        item = save(service, selected)
        assert item["index_state"] == "ready"
        assert service.reindex(selected["profile_id"]) == {"ready": 1, "pending": 0}
        assert api.calls == []


def test_embedding_credential_is_tracked_for_profile_deletion(tmp_path):
    from daeri.profile_deletion import resource_references
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        assert ("credential", "credential:company-embedding") in resource_references(selected)


def test_configure_embedding_requires_inspected_revision_and_preserves_other_settings(tmp_path):
    from daeri.setup import configure_embedding
    with closing(Store(tmp_path / "data")) as store:
        selected = profile(store)
        path = tmp_path / "embedding.json"
        path.write_text(json.dumps(api_config()))
        with pytest.raises(StoreError, match="REVISION_CONFLICT"):
            configure_embedding(store, "alpha", path, expected_revision=selected["revision"] - 1)
        changed = configure_embedding(store, "alpha", path, expected_revision=selected["revision"])
        assert changed["resources"] == selected["resources"]
        assert changed["memory_policy"] == selected["memory_policy"]
        assert changed["embedding"]["provider"] == "api"
        same = configure_embedding(store, "alpha", path, expected_revision=changed["revision"])
        assert same["revision"] == changed["revision"]


def test_cli_configure_status_and_api_prepare_are_explicit(tmp_path):
    from test_cli import run_cli
    directory = tmp_path / "data"
    created = run_cli('--data-dir', str(directory), 'profile', 'create', 'alpha')
    selected = json.loads(created.stdout)
    path = tmp_path / "embedding.json"
    path.write_text(json.dumps(api_config()))
    result = run_cli('--data-dir', str(directory), 'memory', 'configure', 'alpha',
                     '--config', str(path), '--expected-revision', str(selected['revision']))
    assert result.returncode == 0, result.stdout + result.stderr
    status = run_cli('--data-dir', str(directory), 'memory', 'status', '--profile', 'alpha')
    assert status.returncode == 0, status.stdout + status.stderr
    assert json.loads(status.stdout)['provider'] == 'api'
    assert json.loads(status.stdout)['adapter'] == 'openai_compatible'
    assert json.loads(status.stdout)['api_verified'] is False
    prepared = run_cli('--data-dir', str(directory), 'memory', 'prepare', '--profile', 'alpha')
    assert prepared.returncode == 1
    assert not (directory / 'models').exists()


def test_cli_diagnose_only_sends_fixed_probe(tmp_path, api):
    from daeri.cli import _parser, _with_store
    with closing(Store(tmp_path)) as store:
        profile(store, embedding=api_config())
    args = _parser().parse_args(['--data-dir', str(tmp_path), 'memory', 'diagnose', '--profile', 'alpha'])
    assert _with_store(args)['api_verified'] is True
    assert api.calls == [('query', 'company-embedding', 'DAERI embedding readiness')]
