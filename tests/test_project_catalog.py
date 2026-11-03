"""Central project metadata lookup, without AWS credentials or network calls."""

from dataclasses import FrozenInstanceError
import json

import boto3
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
import pytest

from daeri.core.errors import StoreError


ARN = 'arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/daeri-abcdefghij'
METADATA = {'name': 'Charging', 'harness_arn': ARN,
            'harness_endpoint': 'RELEASE_V2', 'harness_version': '2'}
OPERATION = boto3.Session()._session.get_service_model('ssm').operation_model('GetParameter')


class Client:
    def __init__(self, value=None, failure=None):
        self.value = json.dumps(METADATA) if value is None else value
        self.failure, self.requests = failure, []

    def get_parameter(self, **request):
        validate_parameters(request, OPERATION.input_shape)
        self.requests.append(request)
        if self.failure:
            raise self.failure
        response = {'Parameter': {'Name': request['Name'], 'Type': 'String',
                                 'Value': self.value, 'Version': 1}}
        validate_parameters(response, OPERATION.output_shape)
        return response


def catalog(client, prefix='/daeri/projects/'):
    from daeri.infrastructure.project_catalog import SsmProjectCatalog
    return SsmProjectCatalog(client, prefix=prefix)


def test_selected_project_reads_one_central_parameter_and_returns_immutable_metadata():
    client = Client()
    first, second = catalog(client).get('charging'), catalog(client).get('charging')
    assert first.id == 'charging' and first.name == 'Charging' and first.harness_arn == ARN
    assert first.harness_endpoint == 'RELEASE_V2' and first.harness_version == '2'
    assert first == second and first is not second
    assert client.requests == [{'Name': '/daeri/projects/charging', 'WithDecryption': False}] * 2
    with pytest.raises(FrozenInstanceError):
        first.name = 'Changed'


def test_normalized_admin_prefix_selects_the_project():
    client = Client()
    assert catalog(client, '/company/daeri///').get('project_1-dev').id == 'project_1-dev'
    assert client.requests == [{'Name': '/company/daeri/project_1-dev', 'WithDecryption': False}]


@pytest.mark.parametrize('project_id', ['', '../private', 'a/b', '/x', 'a%2fb', ' a', 'a\n', None, 5])
def test_unsafe_project_ids_fail_before_parameter_lookup(project_id):
    client = Client()
    with pytest.raises(StoreError) as error:
        catalog(client).get(project_id)
    assert error.value.code == 'INVALID_INPUT' and client.requests == []


@pytest.mark.parametrize('prefix', ['', '/', 'relative/path', '/daeri/../private', '/a//b', None])
def test_invalid_admin_prefix_is_rejected(prefix):
    with pytest.raises(StoreError) as error:
        catalog(Client(), prefix)
    assert error.value.code == 'INVALID_INPUT'


@pytest.mark.parametrize('metadata', [
    [], {'name': 'Charging'}, {**METADATA, 'token': 'PRIVATE_PAYLOAD'},
    {**METADATA, 'name': ''}, {**METADATA, 'name': 5}, {**METADATA, 'name': 'bad\nname'},
    {**METADATA, 'harness_arn': 'https://arbitrary.example'}, {**METADATA, 'harness_arn': None},
    {**METADATA, 'source_ids': ['jira']}, {**METADATA, 'source_ids': []},
    {**METADATA, 'service': 'legacy'}, {**METADATA, 'profile': 'legacy'},
    {**METADATA, 'name': 'bad\ud800name'},
    {**METADATA, 'name': 'bad\x00name'},
    {**METADATA, 'harness_endpoint': 'DEFAULT'}, {**METADATA, 'harness_endpoint': 'bad/path'},
    {**METADATA, 'harness_version': ''}, {**METADATA, 'harness_version': '0'},
    {**METADATA, 'harness_version': 2}, {**METADATA, 'harness_version': '02'},
])
def test_invalid_metadata_is_sanitized(metadata):
    with pytest.raises(StoreError) as error:
        catalog(Client(json.dumps(metadata))).get('charging')
    assert error.value.code == 'INVALID_INPUT' and 'PRIVATE_PAYLOAD' not in str(error.value)


def test_invalid_json_is_sanitized():
    with pytest.raises(StoreError) as error:
        catalog(Client('PRIVATE_PAYLOAD')).get('charging')
    assert error.value.code == 'INVALID_INPUT' and 'PRIVATE_PAYLOAD' not in str(error.value)


def test_legacy_project_route_requires_a_release_before_it_can_be_used():
    with pytest.raises(StoreError) as error:
        catalog(Client(json.dumps({'name': 'Charging', 'harness_arn': ARN}))).get('charging')
    assert error.value.code == 'INVALID_INPUT'


@pytest.mark.parametrize(('failure', 'code'), [
    (ClientError({'Error': {'Code': 'ParameterNotFound', 'Message': 'PRIVATE_PAYLOAD'}}, 'GetParameter'),
     'PROJECT_UNAVAILABLE'),
    (ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'PRIVATE_PAYLOAD'}}, 'GetParameter'),
     'STORAGE_UNAVAILABLE'),
    (RuntimeError('PRIVATE_PAYLOAD'), 'STORAGE_UNAVAILABLE'),
])
def test_lookup_failures_have_typed_sanitized_errors(failure, code):
    with pytest.raises(StoreError) as error:
        catalog(Client(failure=failure)).get('charging')
    assert error.value.code == code and 'PRIVATE_PAYLOAD' not in str(error.value)
