"""Register native-host source observations and validate their local paths."""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from daeri.core.errors import StoreError
from daeri.infrastructure.adapters.base import ReadAdapter, clean_path, denied


class LocalSourceAdapter(ReadAdapter):
    adapter_id = 'local_source'
    resource_type = 'Source'
    read_tools = frozenset({'source_observe'})
    target_template = {'path': '/SET-ME'}
    setup_fields = {
        'path': {'schema': {'title': '로컬 프로젝트 경로',
                            'description': '호스트가 소스 조회·수정·Git 작업에 사용할 로컬 프로젝트의 절대 디렉터리 경로입니다.',
                            'type': 'string', 'minLength': 1, 'maxLength': 4096,
                            'pattern': r'^(?:/|[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)[^\x00-\x1f\x7f]*$',
                            'examples': ['/workspace/service']},
                 'required': True, 'paths': [['target', 'path']]},
    }

    def validate_connection(self, config):
        if config.get('connection') != {'transport': 'host'}:
            raise StoreError('INVALID_INPUT', 'The local Source adapter uses the native host connection.')
        target = config.get('target', {})
        if not isinstance(target, dict):
            raise StoreError('INVALID_INPUT', 'A local project directory is required.')
        value = target.get('path')
        if (set(target) != {'path'} or not isinstance(value, str) or not value
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise StoreError('INVALID_INPUT', 'A local project directory is required.')
        if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
            raise StoreError('INVALID_INPUT', 'An absolute local project path is required.')

    def _available_root(self, value):
        try:
            path = Path(value)
            if path.is_absolute() and path.is_dir() and os.access(path, os.R_OK | os.X_OK):
                return path.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            pass
        raise StoreError('INVALID_INPUT', 'An existing absolute local project directory is required.')

    def check_available(self, config):
        self.validate_connection(config)
        root = self._available_root(config['target']['path'])
        if config['target']['path'] != str(root):
            raise StoreError('CONTEXT_CHANGED', 'The configured local project path no longer matches its canonical directory.')
        return root

    def normalize_setup_values(self, values):
        result = values.copy()
        if 'path' in result:
            self.validate_connection({'target': {'path': result['path']}, 'connection': {'transport': 'host'}})
            result['path'] = str(self._available_root(result['path']))
        return result

    def finalize_setup_resource(self, resource, *, profile_name):
        self.check_available(resource)
        return resource

    def host_tools(self):
        return [{'name': 'source_observe',
                 'description': 'Register the native host summary of source inspection, edits or Git work; DAERI validates project paths.',
                 'inputSchema': {'type': 'object', 'properties': {
                     'summary': {'type': 'string', 'minLength': 1, 'pattern': r'\S'},
                     'paths': {'type': 'array', 'items': {'type': 'string', 'minLength': 1},
                               'uniqueItems': True, 'maxItems': 100}},
                     'required': ['summary'], 'additionalProperties': False},
                 'outputSchema': {'type': 'object', 'properties': {
                     'path': {'type': 'string'}, 'summary': {'type': 'string'},
                     'paths': {'type': 'array', 'items': {'type': 'string'}},
                     'observation_source': {'const': 'host'}},
                     'required': ['path', 'summary', 'paths', 'observation_source'],
                     'additionalProperties': False},
                 'annotations': {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}}]

    def normalize(self, parameters, constraints, scope):
        result = super().normalize(parameters, constraints, scope)
        paths = parameters.get('paths', [])
        if not isinstance(paths, list):
            denied('Observation paths must be relative project paths.')
        for path in paths:
            clean_path(path)
        return result

    def observe(self, config, parameters):
        from jsonschema import Draft202012Validator
        if not Draft202012Validator(self.host_tools()[0]['inputSchema']).is_valid(parameters):
            raise StoreError('INVALID_INPUT', 'A host summary and bounded relative paths are required.')
        root = self.check_available(config)
        self.normalize(parameters, {}, {})
        paths = parameters.get('paths', [])
        for relative in paths:
            try:
                path = (root / relative).resolve()
                if not path.is_relative_to(root):
                    denied('The observation path leaves the configured local project.')
                if not path.exists():
                    raise StoreError('NOT_FOUND', 'An observed project path does not exist.')
            except (OSError, ValueError, RuntimeError):
                raise StoreError('NOT_FOUND', 'An observed project path is unavailable.') from None
        return {'path': str(root), 'summary': parameters['summary'], 'paths': paths.copy(),
                'observation_source': 'host'}
