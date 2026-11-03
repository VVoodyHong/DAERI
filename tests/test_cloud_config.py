"""Cloud configuration is inert; explicit composition owns its AWS clients."""

import builtins
import importlib

import pytest
from pydantic import ValidationError
from yaml import safe_dump

from daeri.core.errors import StoreError


BASE = {'storage': {'bucket': 'daeri-workspace'}}


def cloud():
    return importlib.import_module('daeri.config.cloud')


def test_reading_yaml_does_not_import_aws_sdk_or_resolve_credentials(tmp_path, monkeypatch):
    original = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name.split('.')[0] in ('boto3', 'botocore'):
            raise AssertionError('Configuration parsing imported the AWS SDK')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_sdk)
    path = tmp_path / 'cloud.yaml'
    path.write_text(safe_dump(BASE))
    settings = cloud().CloudSettings.from_yaml(path)
    assert settings.region is None
    assert settings.project_parameter_prefix == '/daeri/projects/'
    assert settings.storage.bucket == 'daeri-workspace'
    assert settings.storage.prefix == 'daeri/'


@pytest.mark.parametrize('addition', [
    '\nproject_parameter_prefix: /duplicate/\nproject_parameter_prefix: /another/\n',
    '\nunknown: true\n', '\napi_key: do-not-print-this\n',
    '\ndatabase: {name: do-not-print-this}\n',
    '\nknowledge_model: {provider: bedrock, model: legacy}\n',
    '\nembedding: {provider: keyword_only}\n',
    '\nharness_model: {openAiModelConfig: {apiKey: do-not-print-this}}\n',
    '\nstorage: &storage {bucket: daeri-workspace}\n',
    '\nstorage: !!python/object:object {}\n',
])
def test_yaml_rejects_legacy_unknown_duplicate_unsafe_or_secret_values(tmp_path, addition):
    path = tmp_path / 'cloud.yaml'
    path.write_text(safe_dump(BASE) + addition)
    with pytest.raises(StoreError) as error:
        cloud().CloudSettings.from_yaml(path)
    assert error.value.code == 'INVALID_INPUT'
    assert 'do-not-print-this' not in str(error.value)


@pytest.mark.parametrize('storage', [
    {}, {'bucket': ''}, {'bucket': 'ab'}, {'bucket': 'Uppercase'},
    {'bucket': '192.168.1.1'}, {'bucket': 'bad..name'}, {'bucket': 'bad_bucket'},
    {'bucket': '-bad-name'}, {'bucket': 'bad-name-'}, {'bucket': 'bad\x00name'},
    {'bucket': 'daeri-workspace', 'prefix': ''},
    {'bucket': 'daeri-workspace', 'prefix': '/absolute/'},
    {'bucket': 'daeri-workspace', 'prefix': 'a//b'},
    {'bucket': 'daeri-workspace', 'prefix': 'a/../b'},
    {'bucket': 'daeri-workspace', 'prefix': 'a/./b'},
    {'bucket': 'daeri-workspace', 'prefix': 'bad\x00prefix'},
    {'bucket': 'daeri-workspace', 'prefix': 'bad\ud800prefix'},
    {'bucket': 'daeri-workspace', 'unknown': True},
])
def test_storage_settings_reject_invalid_locations_before_creating_clients(storage):
    session = Session()
    with pytest.raises(ValidationError):
        settings = cloud().CloudSettings.model_validate({'storage': storage})
        cloud().build_cloud(settings, session=session)
    assert session.clients == {}


class Client:
    def __init__(self, service):
        self.service, self.requests, self.closed = service, [], False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, fail_service=None):
        self.clients, self.options, self.fail_service = {}, {}, fail_service

    def client(self, service, **options):
        if service == self.fail_service:
            raise RuntimeError('Client creation failed')
        self.options[service] = options
        self.clients[service] = Client(service)
        return self.clients[service]


def test_composition_creates_only_required_clients_without_storage_operations():
    session = Session()
    settings = cloud().CloudSettings.model_validate({**BASE, 'region': 'us-east-1'})
    with cloud().build_cloud(settings, session=session) as services:
        assert services.workflow.store is services.store
        assert services.store.client is session.clients['s3']
        assert services.store.bucket == 'daeri-workspace'
        assert services.store.prefix == 'daeri/'
        assert services.workflow.catalog.client is session.clients['ssm']
        assert services.workflow.catalog.prefix == '/daeri/projects/'
        assert services.workflow.harness.client is session.clients['bedrock-agentcore']
        assert set(session.clients) == {'s3', 'ssm', 'bedrock-agentcore'}
        for service, options in session.options.items():
            assert options['region_name'] == 'us-east-1'
            assert options['config'].retries['total_max_attempts'] == 1
            assert options['config'].connect_timeout == 10
            assert options['config'].read_timeout == (300 if service == 'bedrock-agentcore' else 60)
        assert all(client.requests == [] for client in session.clients.values())
    assert all(client.closed for client in session.clients.values())
    services.close()


def test_partial_factory_failure_closes_previously_created_clients():
    session = Session(fail_service='bedrock-agentcore')
    with pytest.raises(RuntimeError, match='Client creation failed'):
        cloud().build_cloud(cloud().CloudSettings.model_validate(BASE), session=session)
    assert set(session.clients) == {'s3', 'ssm'}
    assert all(client.closed for client in session.clients.values())


@pytest.mark.parametrize('prefix', ['relative', '/', '/daeri/../projects', '/daeri/./projects', '/daeri//projects'])
def test_project_prefix_is_validated_before_creating_clients(prefix):
    session = Session()
    with pytest.raises(ValidationError):
        settings = cloud().CloudSettings.model_validate({**BASE, 'project_parameter_prefix': prefix})
        cloud().build_cloud(settings, session=session)
    assert session.clients == {}


def test_custom_storage_and_project_prefixes_are_normalized_without_resolving_them():
    session = Session()
    settings = cloud().CloudSettings.model_validate({
        'storage': {'bucket': 'daeri-workspace', 'prefix': 'company/daeri'},
        'project_parameter_prefix': '/company/projects'})
    with cloud().build_cloud(settings, session=session) as services:
        assert services.store.prefix == 'company/daeri/'
        assert services.workflow.catalog.prefix == '/company/projects/'
        assert all(options['region_name'] is None for options in session.options.values())
        assert all(client.requests == [] for client in session.clients.values())


def test_missing_sdk_has_actionable_setup_error(monkeypatch):
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
    assert 'uv sync' in error.value.message
