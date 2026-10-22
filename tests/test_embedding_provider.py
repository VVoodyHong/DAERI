import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import httpx
import pytest

from daeri.credentials import Credentials
from daeri.storage import StoreError


def api_config(**changes):
    return {"provider": "api", "adapter": "openai_compatible", "endpoint": "https://embedding.example.test/v1/embeddings",
            "model": "company-deployment", "credential_ref": "env:DAERI_TEST_EMBEDDING_TOKEN",
            "allow_remote": True, **changes}


def provider():
    return importlib.import_module("daeri.embedding")


def test_absent_embedding_config_preserves_local_mode_without_api_options():
    assert provider().validate_embedding_config(None) == {"provider": "local"}
    assert provider().validate_embedding_config({"provider": "keyword_only"}) == {"provider": "keyword_only"}


def test_remote_config_normalizes_defaults_and_keeps_explicit_consent():
    config = api_config()
    config.pop("allow_remote")
    assert provider().validate_embedding_config(config) == {**config, "allow_remote": False, "timeout_seconds": 15}


def test_legacy_provider_uses_the_same_adapter_and_index_identity():
    modern = api_config()
    legacy = {key: value for key, value in modern.items() if key != 'adapter'}
    legacy['provider'] = 'openai_compatible'
    assert provider().validate_embedding_config(legacy) == provider().validate_embedding_config(modern)
    assert provider().OpenAICompatibleEmbedder(legacy).model_id == provider().OpenAICompatibleEmbedder(modern).model_id
    with pytest.raises(StoreError):
        provider().validate_embedding_config({**legacy, 'adapter': 'another-format'})


@pytest.mark.parametrize('adapter', [None, '', 'unregistered', 1])
def test_api_requires_a_registered_connection_adapter(adapter):
    with pytest.raises(StoreError):
        provider().validate_embedding_config(api_config(adapter=adapter))


def test_registered_adapter_owns_its_format_without_openai_fields(monkeypatch):
    module = provider()

    class CompanyFormat:
        label = '사내 전용 API'
        description = '시험용 독자 규격'

        @staticmethod
        def validate_config(config):
            assert set(config) <= {'provider', 'adapter', 'allow_remote', 'deployment'}
            return {**config, 'deployment': config['deployment']}

        @staticmethod
        def draft_config(name):
            return {'provider': 'api', 'adapter': 'company_format', 'deployment': None, 'allow_remote': False}

        def __init__(self, config, **kwargs):
            self.config = config

    monkeypatch.setitem(module.API_ADAPTERS, 'company_format', CompanyFormat)
    config = {'provider': 'api', 'adapter': 'company_format', 'deployment': 'internal'}
    selected = module.validate_embedding_config(config)
    assert selected == {**config, 'allow_remote': False}
    assert module.create_api_embedder(selected).config == selected
    assert module.api_embedding_draft('production', 'company_format')['deployment'] is None
    assert module.api_adapter_catalog()[-1]['label'] == '사내 전용 API'
    with pytest.raises(StoreError):
        module.create_api_embedder({**config, 'allow_remote': 'yes'})


@pytest.mark.parametrize("changes", [
    {"provider": "unknown"}, {"provider": "local"}, {"provider": "keyword_only"},
    {"endpoint": "http://embedding.example.test/v1/embeddings"},
    {"endpoint": "https://user:secret@embedding.example.test/v1/embeddings"},
    {"endpoint": "https://embedding.example.test/v1/embeddings#section"},
    {"endpoint": "https://embedding.example.test/v1/embeddings?api-key=private"},
    {"endpoint": "https://embedding.example.test/v1/embeddings?%74oken=private"},
    {"endpoint": "https://embedding.example.test/v1/embeddings?X-Amz-Signature=private"},
    {"credential_ref": "plaintext-private-token"}, {"credential_ref": "credential:../other"},
    {"allow_remote": "true"}, {"dimensions": True}, {"dimensions": 0}, {"dimensions": 8193},
    {"timeout_seconds": 0}, {"timeout_seconds": 61}, {"timeout_seconds": 1.5},
    {"api_key": "private"}, {"model": "Bearer private"}, {"model": "\ud800"},
])
def test_config_rejects_unsafe_or_ambiguous_values(changes):
    with pytest.raises(StoreError) as error:
        provider().validate_embedding_config(api_config(**changes))
    assert error.value.code == "INVALID_INPUT"
    assert "private" not in str(error.value)


def test_api_uses_exact_endpoint_reference_auth_and_plain_input(monkeypatch):
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.25, -0.5]}], "model": "actual-model"})

    endpoint = "https://embedding.example.test/custom/embeddings?api-version=2025-01"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        embedder = provider().OpenAICompatibleEmbedder(api_config(endpoint=endpoint, dimensions=2), client=client)
        assert embedder.embed_query("검색 질문") == [0.25, -0.5]
        assert embedder.embed_document("검증된 기억") == [0.25, -0.5]
        assert embedder.remote is True
        assert embedder.dimensions == 2
        assert "test-private-token" not in embedder.model_id
        assert "embedding.example" not in embedder.model_id
    assert str(requests[0].url) == endpoint
    assert requests[0].headers["authorization"] == "Bearer test-private-token"
    assert json.loads(requests[0].content) == {"model": "company-deployment", "input": "검색 질문", "encoding_format": "float", "dimensions": 2}
    assert json.loads(requests[1].content)["input"] == "검증된 기억"
    assert requests[0].extensions["timeout"]["read"] == 15


def test_remote_consent_is_required_before_credential_resolution_or_request():
    def unexpected(request):
        pytest.fail("Remote transmission must not occur without consent")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        embedder = provider().OpenAICompatibleEmbedder(api_config(allow_remote=False), client=client)
        with pytest.raises(StoreError) as error:
            embedder.embed_query("must stay local")
    assert error.value.code == "ACCESS_DENIED"


@pytest.mark.parametrize("text", ["", "   ", None, "x" * (65536 + 1)], ids=["empty", "whitespace", "not-text", "oversized"])
def test_invalid_or_oversized_input_is_never_sent(text):
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Invalid input was sent"))) as client:
        embedder = provider().OpenAICompatibleEmbedder(api_config(), client=client)
        with pytest.raises(StoreError, match="MEMORY_INDEX_INCOMPLETE"):
            embedder.embed_query(text)


@pytest.mark.parametrize("body", [
    {}, {"data": []}, {"data": [{"index": 1, "embedding": [1, 2]}]},
    {"data": [{"index": 0, "embedding": []}]},
    {"data": [{"index": 0, "embedding": [0, 0]}]},
    {"data": [{"index": 0, "embedding": [True, 1]}]},
    {"data": [{"index": 0, "embedding": [float("nan"), 1]}]},
    {"data": [{"index": 0, "embedding": [float("inf"), 1]}]},
    {"data": [{"index": 0, "embedding": [1e300, 1]}]},
    {"data": [{"index": 0, "embedding": [1e-300, 1e-300]}]},
    {"data": [{"index": 0, "embedding": [1]}]},
    {"data": [{"index": 0, "embedding": [1, 2]}, {"index": 1, "embedding": [3, 4]}]},
])
def test_malformed_vectors_cannot_enter_the_index(monkeypatch, body):
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(body)))) as client:
        embedder = provider().OpenAICompatibleEmbedder(api_config(dimensions=2), client=client)
        with pytest.raises(StoreError, match="MEMORY_INDEX_INCOMPLETE"):
            embedder.embed_document("private-source-text")


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_error_responses_are_sanitized_and_redirects_are_not_followed(monkeypatch, status):
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://other.example.test/steal"}, text="private-source-text test-private-token")

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        embedder = provider().OpenAICompatibleEmbedder(api_config(), client=client)
        with pytest.raises(StoreError) as error:
            embedder.embed_query("private-source-text")
    assert len(requests) == 1
    assert error.value.code == "MEMORY_INDEX_INCOMPLETE"
    assert "private-source-text" not in str(error.value)
    assert "test-private-token" not in str(error.value)


def test_missing_credentials_and_transport_errors_are_sanitized(monkeypatch):
    monkeypatch.delenv("DAERI_TEST_EMBEDDING_TOKEN", raising=False)
    embedder = provider().OpenAICompatibleEmbedder(api_config(), credentials=Credentials())
    with pytest.raises(StoreError, match="MEMORY_INDEX_INCOMPLETE"):
        embedder.embed_query("private-source-text")
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")

    def timeout(request):
        raise httpx.ReadTimeout("private-source-text test-private-token")

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(StoreError) as error:
            provider().OpenAICompatibleEmbedder(api_config(), client=client).embed_query("private-source-text")
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_response_body_limit_is_checked_before_parsing(monkeypatch):
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b" " * (1024 * 1024 + 1)))) as client:
        with pytest.raises(StoreError, match="MEMORY_INDEX_INCOMPLETE"):
            provider().OpenAICompatibleEmbedder(api_config(), client=client).embed_query("bounded")


def test_model_and_dimensions_changes_do_not_mix_vectors(monkeypatch):
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")
    responses = iter([{"data": [{"index": 0, "embedding": [1, 2]}], "model": "revision-a"},
                      {"data": [{"index": 0, "embedding": [1, 2, 3]}], "model": "revision-b"}])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses)))) as client:
        embedder = provider().OpenAICompatibleEmbedder(api_config(), client=client)
        embedder.embed_query("first")
        assert embedder.dimensions == 2
        identity = embedder.model_id
        with pytest.raises(StoreError, match="MEMORY_INDEX_INCOMPLETE"):
            embedder.embed_document("second")
        assert embedder.model_id == identity


def test_connection_model_and_requested_dimensions_determine_index_identity():
    base = provider().OpenAICompatibleEmbedder(api_config())
    for change in ({"endpoint": "https://other.example.test/embeddings"}, {"model": "different"}, {"dimensions": 2}):
        assert provider().OpenAICompatibleEmbedder(api_config(**change)).model_id != base.model_id
    assert provider().OpenAICompatibleEmbedder(api_config(credential_ref="credential:rotated")).model_id == base.model_id


def test_reported_model_change_is_rejected_even_at_the_same_dimensions(monkeypatch):
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")
    models = iter(["revision-a", "revision-b"])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1, 2]}], "model": next(models)}))) as client:
        embedder = provider().OpenAICompatibleEmbedder(api_config(), client=client)
        embedder.embed_document("first")
        with pytest.raises(StoreError, match="MEMORY_INDEX_INCOMPLETE"):
            embedder.embed_query("second")


def test_streaming_response_is_stopped_at_the_size_limit(monkeypatch):
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")

    class OversizedBody(httpx.SyncByteStream):
        blocks_read = 0

        def __iter__(self):
            for _ in range(100):
                self.blocks_read += 1
                yield b" " * 65536

    stream = OversizedBody()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(StoreError, match="MEMORY_INDEX_INCOMPLETE"):
            provider().OpenAICompatibleEmbedder(api_config(), client=client).embed_query("bounded")
    assert stream.blocks_read == 17


def test_native_store_reference_can_authorize_without_environment_secrets(monkeypatch):
    monkeypatch.delenv("DAERI_TEST_EMBEDDING_TOKEN", raising=False)

    class Vault:
        def get_password(self, service, name):
            assert (service, name) == ("DAERI", "company-embedding")
            return "registered-private-token"

    def respond(request):
        assert request.headers["authorization"] == "Bearer registered-private-token"
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        embedder = provider().OpenAICompatibleEmbedder(
            api_config(credential_ref="credential:company-embedding"), credentials=Credentials(Vault()), client=client)
        assert embedder.embed_document("approved memory") == [1.0, 2.0]


def test_concurrent_first_responses_cannot_establish_conflicting_model_identities(monkeypatch):
    monkeypatch.setenv("DAERI_TEST_EMBEDDING_TOKEN", "test-private-token")
    barrier = Barrier(2)

    class CoordinatedEmbedder(provider().OpenAICompatibleEmbedder):
        def __getattribute__(self, name):
            value = super().__getattribute__(name)
            if name == "_initialized":
                # Force competing initial-state reads; serialization makes the second wait for commit.
                try:
                    barrier.wait(timeout=0.1)
                except BrokenBarrierError:
                    pass
            return value

    def respond(request):
        model = json.loads(request.content)["input"]
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 2]}], "model": model})

    def embed(embedder, text):
        try:
            return embedder.embed_query(text)
        except StoreError as error:
            return error.code

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        embedder = CoordinatedEmbedder(api_config(), client=client)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda text: embed(embedder, text), ["revision-a", "revision-b"]))
    assert results.count("MEMORY_INDEX_INCOMPLETE") == 1
    assert results.count([1.0, 2.0]) == 1
