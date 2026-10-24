"""Exercise profile memory through the actual HTTP embedding adapter without networking."""

from contextlib import closing
import json

import httpx
import pytest

from daeri.credentials import Credentials
from daeri.embedding import OpenAICompatibleEmbedder
from daeri.memory import MemoryService
from daeri.storage import Store, StoreError
from test_embedding_memory import api_config, profile
from test_memory import candidate, memory_policy, save


@pytest.fixture
def company_api(monkeypatch):
    state = {"requests": [], "status": 200, "construct_hook": None}

    class Vault:
        def get_password(self, service, name):
            assert (service, name) == ("DAERI", "company-embedding")
            return "private-company-token"

    def respond(request):
        body = json.loads(request.content)
        state["requests"].append({"url": str(request.url), "body": body, "auth": request.headers["authorization"]})
        if state["status"] != 200:
            return httpx.Response(state["status"], text="private-company-token private-provider-error")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.25]}],
                                         "model": body["model"] + "-resolved"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def create(config, **kwargs):
            hook, state["construct_hook"] = state["construct_hook"], None
            if hook is not None:
                hook()
            return OpenAICompatibleEmbedder(config, credentials=Credentials(Vault()), client=client)

        monkeypatch.setattr("daeri.memory.create_api_embedder", create)
        yield state


def test_http_embedding_indexes_searches_and_reopens_with_matching_identity(tmp_path, company_api):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        item = save(service, selected)
        assert item["index_state"] == "ready"
        found = service.search(selected["profile_id"], "semantic-question-without-keyword-match")
        assert found["retrieval_state"] == "hybrid"
        assert [value["memory_ref"] for value in found["items"]] == [item["memory_ref"]]
        assert company_api["requests"][0]["body"] == {
            "model": "company-embedding", "input": "lesson\nE_CONN_001 오류는 연결 풀 포화 시 확인한다\n운영 DB 연결 문제",
            "encoding_format": "float"}
        assert company_api["requests"][1]["body"]["input"] == "semantic-question-without-keyword-match"
        assert all(request["auth"] == "Bearer private-company-token" for request in company_api["requests"])
        reopened = MemoryService(store)
        result = reopened.search(selected["profile_id"], "another-semantic-question")
        assert [value["memory_ref"] for value in result["items"]] == [item["memory_ref"]]
        assert result["retrieval_state"] == "hybrid"
        assert not (tmp_path / "models").exists()
        assert "private-company-token" not in json.dumps(store.get_profile(selected["profile_id"]))


def test_http_failure_keeps_canonical_memory_and_keyword_search_then_reindexes(tmp_path, company_api):
    company_api["status"] = 503
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        item = save(service, selected)
        assert item["index_state"] == "pending"
        found = service.search(selected["profile_id"], "E_CONN_001")
        assert [value["memory_ref"] for value in found["items"]] == [item["memory_ref"]]
        assert found["retrieval_state"] == "keyword_only"
        assert found["limitations"]
        assert "private-provider-error" not in json.dumps(found)
        company_api["status"] = 200
        assert service.reindex(selected["profile_id"]) == {"ready": 1, "pending": 0}
        assert service.search(selected["profile_id"], "semantic-question")["items"]


def test_http_model_change_requires_reindexing_without_rewriting_memory(tmp_path, company_api):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        item = save(service, selected)
        profile(store, embedding=api_config(model="company-replacement"))
        found = service.search(selected["profile_id"], "semantic-question")
        assert found["items"] == []
        assert found["limitations"]
        assert service.get(selected["profile_id"], item["memory_ref"])["statement"] == candidate()["statement"]
        assert service.reindex(selected["profile_id"]) == {"ready": 1, "pending": 0}
        assert service.search(selected["profile_id"], "semantic-question")["items"]
        assert company_api["requests"][-1]["body"]["model"] == "company-replacement"


def test_switching_to_keyword_search_stops_further_http_and_preserves_memory(tmp_path, company_api):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        item = save(service, selected)
        company_api["requests"].clear()
        profile(store, embedding={'provider': 'keyword_only'})
        found = service.search(selected["profile_id"], "E_CONN_001")
        assert found["retrieval_state"] == "keyword_only"
        assert [value["memory_ref"] for value in found["items"]] == [item["memory_ref"]]
        assert company_api["requests"] == []


@pytest.mark.parametrize("phase", ["index", "search"])
@pytest.mark.parametrize("revoked", ["api_provider", "memory"])
def test_permission_revoked_during_provider_selection_prevents_http_transmission(tmp_path, company_api, phase, revoked):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        if phase == "search":
            save(service, selected)
            profile(store, embedding=api_config(model="force-new-provider"))
        company_api["requests"].clear()

        def revoke():
            current = store.get_profile(selected["profile_id"])
            store.put_profile("alpha", resources=current["resources"],
                              memory_policy=memory_policy(model_allowed=revoked != "memory"),
                              embedding={'provider': 'keyword_only'} if revoked == 'api_provider'
                                        else current['embedding'])

        company_api["construct_hook"] = revoke
        if phase == "index" and revoked == "memory":
            with pytest.raises(StoreError, match="ACCESS_DENIED"):
                save(service, selected)
        elif phase == "index":
            assert save(service, selected)["index_state"] == "pending"
        else:
            service.search(selected["profile_id"], "PRIVATE_QUERY_AFTER_REVOCATION")
        assert company_api["requests"] == []


def test_saved_legacy_config_keeps_profile_revision_files_and_vectors(tmp_path, company_api):
    from daeri.setup import configure_embedding, export_profile
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        legacy = {**selected, 'embedding': dict(selected['embedding'])}
        legacy['embedding'].pop('adapter')
        legacy['embedding']['provider'] = 'openai_compatible'
        path, size, digest = store._prepare('profiles', legacy)
        store._db.execute('UPDATE profiles SET path=?,size=?,sha256=? WHERE profile_id=?',
                          (path, size, digest, selected['profile_id']))
        before = (store.data_dir / path).read_bytes()
        assert store.get_profile('alpha') == selected
        assert export_profile(store, 'alpha')['embedding'] == selected['embedding']
        service = MemoryService(store)
        item = save(service, selected)
        vector = tuple(store._db.execute('SELECT * FROM memory_vectors').fetchone())
        config = tmp_path / 'canonical.json'
        config.write_text(json.dumps(api_config()))
        same = configure_embedding(store, 'alpha', config, expected_revision=selected['revision'])
        assert same == selected
        assert (store.data_dir / path).read_bytes() == before
    with closing(Store(tmp_path)) as store:
        result = MemoryService(store).search(selected['profile_id'], 'semantic-question')
        assert result['retrieval_state'] == 'hybrid'
        assert result['items'][0]['memory_ref'] == item['memory_ref']
        assert tuple(store._db.execute('SELECT * FROM memory_vectors').fetchone()) == vector
        assert store.get_profile('alpha')['revision'] == selected['revision']


@pytest.mark.parametrize('revoke', [False, True])
def test_company_format_uses_registry_and_core_transmission_guard(tmp_path, monkeypatch, revoke):
    from daeri.embedding import API_ADAPTERS
    requests, state = [], {'hook': None}

    def respond(request):
        requests.append(json.loads(request.content))
        assert request.headers['X-Company-Token'] == 'company-test-token'
        return httpx.Response(200, json={'result': {'values': [1.0, 0.25]}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        class CompanyFormat:
            label, description = '사내 전용 API', '시험용 독자 요청·응답 규격'
            dimensions, model_id = 2, 'company-format:test-v1'
            validate_config = staticmethod(dict)

            @staticmethod
            def draft_config(name):
                return {'provider': 'api', 'adapter': 'company_format', 'deployment': None}

            def __init__(self, config, credentials=None, **kwargs):
                self.config = config
                self.credentials = credentials or Credentials()
                hook, state['hook'] = state['hook'], None
                if hook:
                    hook()

            def embed_document(self, text):
                return self._request(text, 'document')

            def embed_query(self, text):
                return self._request(text, 'query')

            def _request(self, text, purpose):
                response = client.post('https://company.example.test/vector',
                    json={'text': text, 'purpose': purpose, 'deployment': self.config['deployment']},
                    headers={'X-Company-Token': self.credentials.get(self.config['credential_ref'])})
                return response.json()['result']['values']

        monkeypatch.setitem(API_ADAPTERS, 'company_format', CompanyFormat)
        monkeypatch.setenv('DAERI_TEST_COMPANY_TOKEN', 'company-test-token')
        with closing(Store(tmp_path)) as store:
            selected = profile(store, embedding={'provider': 'api', 'adapter': 'company_format',
                'deployment': 'internal-model', 'credential_ref': 'env:DAERI_TEST_COMPANY_TOKEN'})
            service = MemoryService(store)
            item = save(service, selected)
            assert item['index_state'] == 'ready'
            assert requests[0]['purpose'] == 'document'
            requests.clear()
            if revoke:
                def withdraw_consent():
                    profile(store, embedding={'provider': 'keyword_only'})
                state['hook'] = withdraw_consent
                service._providers.clear()
            result = service.search(selected['profile_id'], 'semantic-question')
            if revoke:
                assert requests == []
            else:
                assert result['retrieval_state'] == 'hybrid'
                assert result['items'][0]['memory_ref'] == item['memory_ref']
                assert requests == [{'text': 'semantic-question', 'purpose': 'query', 'deployment': 'internal-model'}]
