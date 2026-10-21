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

        monkeypatch.setattr("daeri.memory.OpenAICompatibleEmbedder", create)
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


def test_revoking_remote_permission_stops_further_http_and_preserves_keyword_search(tmp_path, company_api):
    with closing(Store(tmp_path)) as store:
        selected = profile(store, embedding=api_config())
        service = MemoryService(store)
        item = save(service, selected)
        company_api["requests"].clear()
        profile(store, embedding=api_config(allow_remote=False))
        found = service.search(selected["profile_id"], "E_CONN_001")
        assert found["retrieval_state"] == "keyword_only"
        assert [value["memory_ref"] for value in found["items"]] == [item["memory_ref"]]
        assert company_api["requests"] == []


@pytest.mark.parametrize("phase", ["index", "search"])
@pytest.mark.parametrize("revoked", ["remote", "memory"])
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
                              embedding={**current["embedding"], "allow_remote": revoked != "remote"})

        company_api["construct_hook"] = revoke
        if phase == "index" and revoked == "memory":
            with pytest.raises(StoreError, match="ACCESS_DENIED"):
                save(service, selected)
        elif phase == "index":
            assert save(service, selected)["index_state"] == "pending"
        else:
            service.search(selected["profile_id"], "PRIVATE_QUERY_AFTER_REVOCATION")
        assert company_api["requests"] == []
