"""Validate or explicitly publish trusted connector templates and project bindings."""

import argparse
import json
import sys
from typing import Annotated

from botocore.exceptions import ClientError, PaginationError
from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

from daeri.config.connections import (
    ConnectionId, ConnectionSettings, ConnectorTemplate, ProjectConnections, validate_project_bindings,
)
from daeri.config.yaml import read_yaml
from daeri.core.errors import StoreError
from daeri.core.serialization import validate_storable_json
from daeri.infrastructure.aws_pagination import pages


class CatalogSettings(ConnectionSettings):
    region: Annotated[str, StringConstraints(pattern=r'^[a-z]{2}(?:-[a-z]+)+-[0-9]+$')] | None = None
    connectors: dict[ConnectionId, ConnectorTemplate] = Field(default_factory=dict)
    projects: dict[ConnectionId, ProjectConnections] = Field(default_factory=dict)


def _documents(settings):
    documents = []
    for section, records in (('catalog', settings.connectors), ('projects', settings.projects)):
        for identifier, record in sorted(records.items()):
            data = validate_storable_json(record.model_dump(mode='json', exclude_unset=True))
            if len(data) > 8192:
                raise ValueError('Parameter Store documents must fit the advanced parameter limit.')
            documents.append({'Name': settings.prefix + section + '/' + identifier,
                              'Value': data.decode('utf-8'), 'Type': 'String',
                              'Overwrite': True, 'Tier': 'Intelligent-Tiering'})
    return documents


def _stored_projects(client, prefix, replacements):
    projects, path = {}, prefix + 'projects/'
    identifier = TypeAdapter(ConnectionId)
    for response in pages(client, 'get_parameters_by_path', token_field='NextToken',
                          Path=path, Recursive=False, WithDecryption=False, MaxResults=10):
        for parameter in response.get('Parameters', []):
            name = parameter['Name']
            if not name.startswith(path):
                raise ValueError('Unexpected project parameter path.')
            project_id = identifier.validate_python(name[len(path):])
            if project_id not in replacements:
                projects[project_id] = ProjectConnections.model_validate_json(parameter['Value'])
    return projects


def _stored_connector(client, prefix, connector_id):
    try:
        response = client.get_parameter(Name=prefix + 'catalog/' + connector_id, WithDecryption=False)
    except ClientError as error:
        if error.response['Error']['Code'] == 'ParameterNotFound':
            return None
        raise
    return ConnectorTemplate.model_validate_json(response['Parameter']['Value'])


def _validate_runtime_roles(projects, connectors, *, read_connector=None):
    unbound = {connector_id for project in projects.values() if project.runtime_role_arn is None
               for connector_id in project.allowed_connectors}
    for connector_id in sorted(unbound):
        template = connectors.get(connector_id)
        if template is None and read_connector is not None:
            template = read_connector(connector_id)
        if template is not None and template.kind == 'runtime':
            raise ValueError('Projects allowing Runtime connectors require an execution role.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--apply', action='store_true', help='Publish the validated templates and project bindings.')
    arguments = parser.parse_args(argv)
    try:
        settings = CatalogSettings.model_validate(read_yaml(arguments.config))
        validate_project_bindings(settings.projects)
        _validate_runtime_roles(settings.projects, settings.connectors)
        documents = _documents(settings)
        if arguments.apply:
            import boto3
            from botocore.config import Config

            with boto3.client('ssm', region_name=settings.region, config=Config(
                    retries={'total_max_attempts': 1}, connect_timeout=10, read_timeout=60)) as ssm:
                projects = _stored_projects(ssm, settings.prefix, settings.projects)
                _validate_runtime_roles(projects, settings.connectors)
                _validate_runtime_roles(settings.projects, settings.connectors,
                                        read_connector=lambda identifier: _stored_connector(ssm, settings.prefix, identifier))
                projects.update(settings.projects)
                validate_project_bindings(projects)
                for document in documents:
                    ssm.put_parameter(**document)
        print(json.dumps({'applied': arguments.apply, 'connectors': sorted(settings.connectors),
                          'projects': sorted(settings.projects)}))
        return 0
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError, PaginationError):
        error = StoreError('INVALID_INPUT', 'Check the connector templates, project roles, ownership and parameter size limits.')
    except StoreError:
        error = StoreError('INVALID_INPUT', 'Check the connector catalog YAML and supported settings.')
    except Exception:
        error = StoreError('DEPLOYMENT_FAILED', 'Connection catalog publication failed; check access and current parameter state.')
    print(json.dumps({'error': {'code': error.code, 'message': error.message}}), file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
