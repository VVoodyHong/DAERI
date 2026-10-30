"""Cloud configuration is inert; explicit composition owns its AWS clients."""

import builtins
import importlib
import io
import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from daeri.config.setup_yaml import write_yaml
from daeri.core.errors import StoreError


SECRET = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:company-key'
IDENTITY_KEY = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/apikeycredentialprovider/company-key'
HARNESS = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/assistant-1234567890'
BASE = {
    'harness_arn': HARNESS,
    'database': {'resource_arn': 'arn:aws:rds:us-east-1:123456789012:cluster:shared',
                 'secret_arn': SECRET, 'name': 'daeri'},
    'knowledge_model': {'provider': 'bedrock', 'model': 'summary-model'},
}


def cloud():
    return importlib.import_module('daeri.config.cloud')


def api(endpoint):
    return {'provider': 'api', 'endpoint': endpoint, 'model': 'company-model',
            'api_key_secret_arn': SECRET}


def test_reading_yaml_does_not_import_aws_sdk_or_resolve_credentials(tmp_path, monkeypatch):
    original = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name.split('.')[0] in ('boto3', 'botocore'):
            raise AssertionError('Configuration parsing imported the AWS SDK')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_sdk)
    path = tmp_path / 'cloud.yaml'
    path.write_text(write_yaml(BASE))
    settings = cloud().CloudSettings.from_yaml(path)
    assert settings.region is None
    assert settings.harness_model is None
    assert settings.embedding.provider == 'keyword_only'
    assert settings.database.name == 'daeri'


@pytest.mark.parametrize('addition', [
    '\nharness_arn: duplicate\n', '\nunknown: true\n', '\napi_key: do-not-print-this\n',
    '\nembedding: {provider: api, endpoint: https://company.test/v1/embeddings, model: chosen, api_key: do-not-print-this}\n',
    '\nharness_model: {openAiModelConfig: {modelId: chosen, apiKey: do-not-print-this}}\n',
    '\nharness_model: {openAiModelConfig: {modelId: chosen, apiKeyArn: do-not-print-this}}\n',
    '\nembedding: {provider: keyword_only, dimensions: 2}\n',
    '\nembedding: {provider: bedrock, dimensions: 384}\n',
    '\nembedding: {provider: bedrock, dimensions: true}\n',
    '\nembedding: {provider: bedrock, model: unsupported}\n',
    '\nembedding: {provider: local}\n',
    '\nembedding: &embedding {provider: keyword_only}\n',
    '\nembedding: !!python/object:object {}\n',
])
def test_yaml_rejects_unknown_duplicate_unsafe_or_secret_values(tmp_path, addition):
    path = tmp_path / 'cloud.yaml'
    path.write_text(write_yaml(BASE) + addition)
    with pytest.raises(StoreError) as error:
        cloud().CloudSettings.from_yaml(path)
    assert error.value.code == 'INVALID_INPUT'
    assert 'do-not-print-this' not in str(error.value)


@pytest.mark.parametrize('endpoint', ['http://company.test/v1/chat/completions',
                                      'https://user:password@company.test/v1/chat/completions',
                                      'https://company.test/',
                                      'https://company.test/v1/chat/completions?api_key=secret'])
def test_cloud_api_endpoints_require_explicit_nonsecret_https_request_url(tmp_path, endpoint):
    settings = {**BASE, 'knowledge_model': api(endpoint)}
    path = tmp_path / 'cloud.yaml'
    path.write_text(write_yaml(settings))
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        cloud().CloudSettings.from_yaml(path)


@pytest.mark.parametrize('model', ['', ' summary', 'summary ', '\x00summary', '\x7f', 'summary\n'])
def test_model_identifiers_reject_empty_whitespace_or_control_characters(tmp_path, model):
    path = tmp_path / 'cloud.yaml'
    path.write_text(write_yaml({**BASE, 'knowledge_model': {'provider': 'bedrock', 'model': model}}))
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        cloud().CloudSettings.from_yaml(path)


class Client:
    def __init__(self, service):
        self.service, self.requests, self.closed = service, [], False

    def close(self):
        self.closed = True

    def get_secret_value(self, **request):
        self.requests.append(request)
        return {'SecretString': 'resolved-company-token'}

    def invoke_model(self, **request):
        self.requests.append(request)
        dimensions = json.loads(request['body'])['dimensions']
        return {'body': io.BytesIO(json.dumps({'embedding': [1.] * dimensions}).encode())}

    def converse(self, **request):
        self.requests.append(request)
        return {'stopReason': 'end_turn', 'output': {'message': {'role': 'assistant',
                'content': [{'text': 'Observed experience.'}]}}}


class Session:
    def __init__(self, fail_service=None):
        self.clients, self.options, self.fail_service = {}, {}, fail_service

    def client(self, service, **options):
        if service == self.fail_service:
            raise RuntimeError('Client creation failed')
        self.options[service] = options
        self.clients[service] = Client(service)
        return self.clients[service]


def require_sdk():
    pytest.importorskip('boto3')


def test_composition_disables_aws_retries_and_leaves_initialization_explicit():
    require_sdk()
    session = Session()
    settings = cloud().CloudSettings.model_validate({**BASE, 'region': 'us-east-1'})
    with cloud().build_cloud(settings, session=session) as services:
        assert services.workflow.store is services.knowledge.store is services.store
        assert services.workflow.embedder is services.knowledge.embedder is None
        assert services.workflow.harness.harness_arn == HARNESS
        assert services.workflow.harness.model is None
        assert services.store.connection == {
            'resourceArn': BASE['database']['resource_arn'], 'secretArn': SECRET, 'database': 'daeri'}
        assert services.knowledge.extractor.extract('request', 'result') == 'Observed experience.'
        for options in session.options.values():
            assert options['region_name'] == 'us-east-1'
            assert options['config'].retries['total_max_attempts'] == 1
            assert options['config'].connect_timeout == 10
            assert options['config'].read_timeout > 0
        assert session.clients['rds-data'].requests == []
        assert session.clients['bedrock-agentcore'].requests == []
    assert all(client.closed for client in session.clients.values())
    services.close()


@pytest.mark.parametrize('knowledge_api', [True, False])
def test_knowledge_and_embedding_providers_are_independent_and_secrets_resolve_on_request(knowledge_api):
    require_sdk()
    session, requests = Session(), []

    def respond(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer resolved-company-token'
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1., 0.]}]})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                              'message': {'content': 'Company experience.'}}]})

    config = deepcopy(BASE)
    config['knowledge_model'] = (api('https://company.test/exact/chat/completions')
                                 if knowledge_api else BASE['knowledge_model'])
    config['embedding'] = ({'provider': 'bedrock', 'dimensions': 256} if knowledge_api
                           else {**api('https://company.test/exact/embeddings'), 'dimensions': 2})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with cloud().build_cloud(cloud().CloudSettings.model_validate(config),
                                 session=session, http_client=http) as services:
            secrets = session.clients['secretsmanager']
            assert secrets.requests == [] and requests == []
            assert session.options['rds-data']['region_name'] is None
            summary = services.knowledge.extractor.extract('request', 'result')
            assert summary == ('Company experience.' if knowledge_api else 'Observed experience.')
            vector = services.workflow.embedder.embed_query('query')
            assert vector == ([1.] * 256 if knowledge_api else [1., 0.])
            assert secrets.requests == [{'SecretId': SECRET}]
            assert str(requests[0].url) == ('https://company.test/exact/chat/completions'
                    if knowledge_api else 'https://company.test/exact/embeddings')
        assert not http.is_closed
    assert all(client.closed for client in session.clients.values())


def test_factory_closes_owned_http_and_clients_after_use():
    require_sdk()
    session = Session()
    config = {**BASE, 'knowledge_model': api('https://company.test/v1/chat/completions')}
    with cloud().build_cloud(cloud().CloudSettings.model_validate(config), session=session) as services:
        http = services.knowledge.extractor.client
        assert not http.is_closed
    assert http.is_closed
    assert all(client.closed for client in session.clients.values())


def test_partial_factory_failure_closes_previously_created_clients():
    require_sdk()
    session = Session(fail_service='bedrock-runtime')
    with pytest.raises(RuntimeError, match='Client creation failed'):
        cloud().build_cloud(cloud().CloudSettings.model_validate(BASE), session=session)
    assert session.clients
    assert all(client.closed for client in session.clients.values())


@pytest.mark.parametrize('model', [{}, {'bedrockModelConfig': {'modelId': 'chosen', 'unknown': 1}},
                                  {'openAiModelConfig': {'modelId': 'chosen'}},
                                  {'bedrockModelConfig': {'modelId': 'chosen'},
                                   'openAiModelConfig': {'modelId': 'chosen', 'apiKeyArn': IDENTITY_KEY}},
                                  {'openAiModelConfig': {'modelId': 'chosen', 'apiKeyArn': IDENTITY_KEY,
                                   'endpoint': {'bedrockMantle': {}}}}])
def test_harness_override_is_validated_against_sdk_shape_before_creating_clients(model):
    require_sdk()
    session = Session()
    settings = cloud().CloudSettings.model_validate({**BASE, 'harness_model': model})
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        cloud().build_cloud(settings, session=session)
    assert session.clients == {}


def test_native_harness_model_uses_identity_key_without_resolving_it_in_daeri():
    require_sdk()
    session = Session()
    model = {'openAiModelConfig': {'modelId': 'company-chat', 'apiKeyArn': IDENTITY_KEY,
                                 'apiBase': 'https://company.test/v1', 'apiFormat': 'chat_completions'}}
    settings = cloud().CloudSettings.model_validate({**BASE, 'harness_model': model})
    with cloud().build_cloud(settings, session=session) as services:
        assert services.workflow.harness.model == model
        assert services.workflow.embedder is None
        assert services.knowledge.extractor.model_id == 'summary-model'
        assert 'secretsmanager' not in session.clients


@pytest.mark.parametrize('provider', ['openAiModelConfig', 'geminiModelConfig', 'liteLlmModelConfig'])
@pytest.mark.parametrize('reference', [SECRET,
    'arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default',
    'arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/company-key'])
def test_harness_api_key_requires_identity_api_key_provider_reference(provider, reference):
    with pytest.raises(ValidationError, match='AgentCore Identity'):
        cloud().CloudSettings.model_validate({**BASE, 'harness_model': {
            provider: {'modelId': 'company-chat', 'apiKeyArn': reference}}})


def test_missing_optional_sdk_has_actionable_setup_error(monkeypatch):
    module = cloud()
    original = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name.split('.')[0] == 'boto3':
            raise ModuleNotFoundError('boto3 unavailable')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_sdk)
    with pytest.raises(StoreError) as error:
        module.build_cloud(module.CloudSettings.model_validate(BASE))
    assert error.value.code == 'SETUP_REQUIRED'
    assert 'cloud' in error.value.message
