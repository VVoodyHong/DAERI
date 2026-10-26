"""Jira tenant-bound token connections and approved issue/project/JQL reads."""

import re
from urllib.parse import urlsplit

from daeri.infrastructure.adapters.base import ReadAdapter, denied
from daeri.infrastructure.credentials import resource_reference
from daeri.core.errors import StoreError


class JiraReadAdapter(ReadAdapter):
    adapter_id = 'jira_read'
    resource_type = 'Issue'
    discover_annotations = True
    tool_prefix = 'jira_'
    read_only_environment = 'READ_ONLY_MODE'
    read_tools = frozenset({'jira_get_issue', 'jira_get_project_issues',
                            'jira_get_transitions', 'jira_search'})
    target_template = {'server': 'https://SET-ME.invalid'}
    setup_fields = {
        'server': {'schema': {
            'title': '이슈 서버 주소', 'description': '접속할 Jira의 HTTPS 주소입니다. 도메인이 다른 Jira는 별도 연결로 추가합니다.',
            'type': 'string', 'minLength': 1, 'maxLength': 2048,
            'pattern': r'^https://[^/?#@\s\\\x00-\x1f\x7f]+(?:/[^?#\s\\\x00-\x1f\x7f]*)?$',
            'examples': ['https://issues.example.test']},
            'required': True, 'paths': [['target', 'server'], ['connection', 'env', 'JIRA_URL']]},
        'auth_type': {'schema': {
            'title': '이슈 인증 방식', 'description': 'Cloud는 api_token, Server/Data Center의 개인 액세스 토큰은 personal_token을 선택합니다.',
            'type': 'string', 'enum': ['api_token', 'personal_token'], 'default': 'api_token',
            'examples': ['api_token']},
            'required': True, 'paths': [['connection', 'env', 'DAERI_JIRA_AUTH_TYPE']]},
        'username': {'schema': {
            'title': '이슈 인증 계정', 'description': 'api_token 인증에 사용할 계정입니다. personal_token 인증은 빈 문자열을 사용합니다.',
            'type': 'string', 'maxLength': 320, 'pattern': r'^[^\x00-\x1f\x7f]*$', 'default': '',
            'examples': ['reader@example.test']},
            'required': False, 'paths': [['connection', 'env', 'JIRA_USERNAME']]},
    }
    environment_prefixes = ('JIRA_', 'CONFLUENCE_', 'ATLASSIAN_', 'OAUTH_', 'DAERI_JIRA_')
    control_fields = frozenset({'READ_ONLY_MODE', 'ENABLED_TOOLS'})
    authentication_fields = frozenset({'JIRA_API_TOKEN', 'JIRA_PERSONAL_TOKEN'})
    setup_credentials = {name: {'field': 'token', 'description': '이슈 서비스 접속에 사용할 토큰입니다.'}
                         for name in authentication_fields}
    executable = 'mcp-atlassian'
    explicit_credentials = True
    normalize_results = True
    decode_result_strings = True
    preserve_inner_schema = False

    def finalize_setup_resource(self, resource, *, profile_name):
        connection = resource['connection']
        kind = connection['env'].get('DAERI_JIRA_AUTH_TYPE', 'api_token')
        selected = 'JIRA_PERSONAL_TOKEN' if kind == 'personal_token' else 'JIRA_API_TOKEN'
        references = connection.get('auth_env', {})
        if selected not in references and len(references) == 1:
            previous, reference = next(iter(references.items()))
            if previous in self.authentication_fields:
                # Preserve custom references; generated references follow the selected token kind.
                if reference == resource_reference(profile_name, resource['instance_id'], previous):
                    reference = resource_reference(profile_name, resource['instance_id'], selected)
                connection['auth_env'] = {selected: reference}
        return resource

    def validate_connection(self, config):
        super().validate_connection(config)
        connection = config['connection']
        if connection['transport'] != 'stdio':
            return
        env = connection.get('env', {})
        allowed = {'JIRA_URL', 'JIRA_USERNAME', 'DAERI_JIRA_AUTH_TYPE', 'JIRA_SSL_VERIFY',
                   'READ_ONLY_MODE', 'ENABLED_TOOLS'}
        if set(env) - allowed:
            raise StoreError('UNSUPPORTED_CONSTRAINT', 'Jira connections use the registered server, token authentication, and read-only controls.')
        try:
            url = urlsplit(env.get('JIRA_URL', ''))
            valid_url = (url.scheme == 'https' and bool(url.hostname) and not url.username
                         and not url.password and not url.query and not url.fragment
                         and not any(char.isspace() or ord(char) < 32 or char == '\\' for char in env.get('JIRA_URL', '')))
            url.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise StoreError('INVALID_INPUT', 'Jira requires an explicit HTTPS server without credentials or query parameters.')
        if env.get('READ_ONLY_MODE') != 'true' or env.get('JIRA_SSL_VERIFY', 'true').lower() not in {'true', '1'}:
            raise StoreError('UNSUPPORTED_CONSTRAINT', 'Jira reads require read-only mode and server certificate verification.')
        if 'ENABLED_TOOLS' in env:
            selected = env['ENABLED_TOOLS'].split(',')
            configured = {item['tool_name'] for item in config.get('capabilities', [])}
            if not selected or any(name not in self.read_tools for name in selected) or not configured <= set(selected):
                raise StoreError('UNSUPPORTED_CONSTRAINT', 'Enable the registered Jira read tools for the selected capabilities.')
        kind = env.get('DAERI_JIRA_AUTH_TYPE', 'personal_token' if 'JIRA_PERSONAL_TOKEN' in connection.get('auth_env', {}) else 'api_token')
        expected = 'JIRA_PERSONAL_TOKEN' if kind == 'personal_token' else 'JIRA_API_TOKEN'
        if kind not in {'api_token', 'personal_token'} or set(connection.get('auth_env', {})) != {expected}:
            raise StoreError('AUTHENTICATION_REQUIRED', 'Jira requires one token reference matching the selected authentication type.')
        if kind == 'api_token' and not env.get('JIRA_USERNAME', '').strip():
            raise StoreError('INVALID_INPUT', 'Jira API token authentication requires its account username.')
        cloud = url.hostname.endswith(('.atlassian.net', '.jira.com', '.jira-dev.com', '.atlassian.com',
                                       '.atlassian-us-gov-mod.net', '.atlassian-us-gov.net'))
        if kind == 'personal_token' and cloud:
            raise StoreError('UNSUPPORTED_CONSTRAINT', 'Jira Cloud uses an account username and API token.')

    def launch_connection(self, config):
        connection = super().launch_connection(config)
        if connection['transport'] == 'stdio':
            connection['env'].pop('DAERI_JIRA_AUTH_TYPE', None)
            if 'JIRA_PERSONAL_TOKEN' in connection.get('auth_env', {}):
                connection['env'].pop('JIRA_USERNAME', None)
        return connection

    def binding_files(self, config, credentials):
        connection, target = config['connection'], config['target']
        if connection['transport'] == 'stdio':
            if (not isinstance(target.get('server'), str) or not target['server']
                    or connection.get('env', {}).get('JIRA_URL', '').rstrip('/') != target['server'].rstrip('/')):
                raise StoreError('CONTEXT_CHANGED', 'The Jira connection server differs from the selected resource target.')
        return {}

    def prepare_parameters(self, parameters, input_schema):
        # The upstream tool defaults to updating view history; normalize to the read contract.
        if 'update_history' in input_schema.get('properties', {}):
            parameters['update_history'] = False

    def normalize(self, parameters, constraints, scope):
        result = super().normalize(parameters, constraints, scope)
        if any(field in parameters for field in ('url', 'server', 'base_url', 'cloud_id', 'site_url')):
            denied('The selected Jira connection supplies the server; use explicit issue or project keys.')
        for field, pattern in (('issue_key', r'[A-Z][A-Z0-9_]+-[0-9]+(?:-[0-9]+)*'),
                               ('project_key', r'[A-Z][A-Z0-9_]+')):
            if field in parameters and (not isinstance(parameters[field], str) or re.fullmatch(pattern, parameters[field]) is None):
                denied('Jira reads require an explicit issue or project key.')
        if 'update_history' in parameters and parameters['update_history'] is not False:
            denied('Jira read requests require history updates disabled.')
        return result
