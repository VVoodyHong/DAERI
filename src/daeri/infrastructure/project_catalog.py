"""Read administrator-managed project references from SSM Parameter Store."""

import json
import re

from daeri.core.errors import StoreError
from daeri.core.projects import Project, valid_harness_release
from daeri.core.serialization import validate_storable_json


_PROJECT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
_HARNESS_ARN = re.compile(
    r'arn:[a-z0-9-]+:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:'
    r'harness/[A-Za-z][A-Za-z0-9_]{0,39}-[A-Za-z0-9]{10}')


def normalize_project_prefix(prefix: str) -> str:
    normalized = prefix.rstrip('/') if isinstance(prefix, str) else ''
    if (not re.fullmatch(r'/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', normalized)
            or any(part in {'.', '..'} for part in normalized.split('/'))):
        raise StoreError('INVALID_INPUT', 'Supply an absolute project parameter prefix.')
    return normalized + '/'


class SsmProjectCatalog:
    """A lookup only; callers must first verify project-wide shared-data access."""

    def __init__(self, client, prefix='/daeri/projects/'):
        self.client, self.prefix = client, normalize_project_prefix(prefix)

    def get(self, project_id: str) -> Project:
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            raise StoreError('INVALID_INPUT', 'Supply a project ID using letters, digits, underscores or hyphens.')
        try:
            result = self.client.get_parameter(Name=self.prefix + project_id, WithDecryption=False)
        except Exception as error:
            response = getattr(error, 'response', None)
            details = response.get('Error') if isinstance(response, dict) else None
            if isinstance(details, dict) and details.get('Code') == 'ParameterNotFound':
                raise StoreError('PROJECT_UNAVAILABLE', 'The selected project is unavailable.') from None
            raise StoreError('STORAGE_UNAVAILABLE', 'The project catalog could not be read.') from None
        try:
            metadata = json.loads(result['Parameter']['Value'])
            validate_storable_json(metadata)
            if not isinstance(metadata, dict) or set(metadata) != {
                    'name', 'harness_arn', 'harness_endpoint', 'harness_version'}:
                raise ValueError('Unsupported project fields')
            name, arn = metadata['name'], metadata['harness_arn']
            endpoint, version = metadata['harness_endpoint'], metadata['harness_version']
            if (not isinstance(name, str) or not 1 <= len(name) <= 256 or name.strip() != name
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                    or not isinstance(arn, str) or not _HARNESS_ARN.fullmatch(arn)
                    or not valid_harness_release(endpoint, version)):
                raise ValueError('Invalid project metadata')
            return Project(project_id, name, arn, endpoint, version)
        except (KeyError, TypeError, ValueError, RecursionError):
            raise StoreError('INVALID_INPUT', 'The stored project metadata is invalid.') from None
