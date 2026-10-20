"""Explicit local configuration and pinned lower-MCP preparation; no implicit login."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from uuid import uuid4

from .adapters import get_adapter
from .security import redact
from .storage import StoreError


PROFILE_FIELDS = {'name', 'core_context', 'resources', 'retention_days',
                  'long_term_memory_days', 'memory_policy'}
REFERENCE = re.compile(r'(credential:[A-Za-z0-9._-]{1,128}|env:[A-Za-z_][A-Za-z0-9_]*)')
SECRET_KEYS = {'password', 'passwd', 'token', 'api_key', 'secret', 'access_token',
               'refresh_token', 'private_key', 'client_secret', 'authorization'}


def _require(value, message='Local configuration is invalid.'):
    if not value:
        raise StoreError('INVALID_INPUT', message)


def _pairs(values):
    result = {}
    for key, value in values:
        _require(key not in result, 'Duplicate JSON object keys are not allowed.')
        result[key] = value
    return result


def read_config(path):
    try:
        with Path(path).open('rb') as stream:
            content = stream.read(2 * 1024 * 1024 + 1)
        _require(len(content) <= 2 * 1024 * 1024, 'Configuration exceeds its size limit.')
        value = json.loads(content.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        json.dumps(value, allow_nan=False)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise StoreError('INVALID_INPUT', 'A bounded, valid UTF-8 JSON configuration file is required.') from None
    def depth(node, level=0):
        _require(level <= 32, 'Configuration exceeds its nesting limit.')
        if isinstance(node, dict):
            for item in node.values(): depth(item, level + 1)
        elif isinstance(node, list):
            for item in node: depth(item, level + 1)
    depth(value)
    return value


def _no_secrets(value, path=()):
    _require(redact(value) == value, 'Use authentication references instead of credential values.')
    if isinstance(value, dict):
        for key, item in value.items():
            if path and path[-1] in {'auth_env', 'auth_headers'}:
                _require(isinstance(item, str) and bool(REFERENCE.fullmatch(item)), 'Use a supported credential reference.')
            else:
                _require(key.lower() not in SECRET_KEYS, 'Plaintext credential fields are not allowed.')
                _no_secrets(item, path + (key,))
    elif isinstance(value, list):
        for item in value:
            if path and path[-1] == 'args' and isinstance(item, str):
                _require(not re.match(r'--?(password|passwd|token|secret|api[-_]key|authorization|database[-_]uri)(=|$)', item, re.I),
                         'Credentials cannot be supplied as process arguments.')
            _no_secrets(item, path)


def import_profile(store, path):
    config = read_config(path)
    _require(isinstance(config, dict) and set(config) <= PROFILE_FIELDS and 'name' in config)
    _no_secrets(config)
    return store.put_profile(**config)


def export_profile(store, name):
    profile = store.get_profile(name)
    value = {key: copy.deepcopy(profile[key]) for key in PROFILE_FIELDS}
    value['memory_policy'].pop('target_bindings', None)
    _no_secrets(value)
    return value


def preview_profile_deletion(store, name_or_id):
    from .profile_deletion import preview
    return preview(store, name_or_id)


def delete_profile(store, name_or_id, confirmation, *, credentials=None):
    from .credentials import Credentials
    from .profile_deletion import delete
    return delete(store, name_or_id, confirmation, credentials or Credentials())


def load_catalog():
    return json.loads(Path(__file__).with_name('catalog.json').read_text(encoding='utf-8'))


def _recipe(recipe_id):
    catalog = load_catalog()
    _require(recipe_id in catalog, 'Select a registered preparation recipe.')
    item = catalog[recipe_id]
    _require(item['type'] == get_adapter(item['adapter']).resource_type,
             'The preparation recipe resource type differs from its registered adapter.')
    bundled = Path(__file__).with_name('recipes')
    root = bundled if bundled.is_dir() else Path(__file__).resolve().parents[2] / 'recipes'
    project = root / recipe_id
    try:
        metadata = tomllib.loads((project / 'pyproject.toml').read_text(encoding='utf-8'))
        lock = (project / 'uv.lock').read_bytes()
    except (OSError, ValueError):
        raise StoreError('CONNECTION_NOT_READY', 'The pinned resource preparation recipe is unavailable.') from None
    _require(metadata.get('project', {}).get('dependencies') == [item['package'] + '==' + item['version']],
             'The preparation recipe and catalog version do not match.')
    return item, project, hashlib.sha256(lock).hexdigest()


def _tool_directory(store, recipe_id):
    item, project, lock_hash = _recipe(recipe_id)
    return store.data_dir / 'tools' / recipe_id / (item['version'] + '-' + lock_hash[:16])


def _program(environment, executable):
    return environment / ('Scripts' if os.name == 'nt' else 'bin') / (executable + '.exe' if os.name == 'nt' else executable)


def prepared_executable(store, recipe_id):
    return _program(_tool_directory(store, recipe_id) / '.venv', load_catalog()[recipe_id]['executable']).resolve()


def profile_template(store, recipe_id, profile_name, instance_id):
    item, _, _ = _recipe(recipe_id)
    _require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile_name)))
    _require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', instance_id)))
    resource = {'instance_id': instance_id, 'type': item['type'], 'adapter': item['adapter'],
                'target': copy.deepcopy(get_adapter(item['adapter']).target_template),
                'identity_ref': 'identity:' + profile_name + ':' + instance_id,
                'connection': {'transport': 'stdio', 'command': str(prepared_executable(store, recipe_id)),
                               'args': copy.deepcopy(item['args']), 'env': copy.deepcopy(item['env']),
                               'auth_env': {name: 'credential:' + profile_name + '.' + instance_id + '.' + name.lower()
                                            for name in item['auth_env']}},
                'capabilities': copy.deepcopy(item['capabilities']), 'policy': {}}
    return {'name': profile_name, 'core_context': [], 'resources': [resource],
            'retention_days': None, 'long_term_memory_days': None, 'memory_policy': {}}


def setup_schema(recipe_id):
    """Describe trusted non-secret inputs; defaults are hints, never answers."""
    from jsonschema import Draft202012Validator
    item, _, _ = _recipe(recipe_id)
    adapter = get_adapter(item['adapter'])
    fields = adapter.setup_fields
    _require(bool(fields), 'The selected adapter has no structured setup contract.')
    schema = {'type': 'object', 'properties': {
        name: copy.deepcopy(field['schema']) for name, field in fields.items()},
        'required': [name for name, field in fields.items() if field['required']],
        'additionalProperties': False}
    Draft202012Validator.check_schema(schema)
    return {'recipe_id': recipe_id, 'adapter': adapter.adapter_id, 'type': item['type'],
            'display_name': item['display_name'], 'input_schema': schema,
            'configuration_paths': {name: copy.deepcopy(field['paths']) for name, field in fields.items()},
            'authentication': {'credential_environment': copy.deepcopy(item['auth_env']),
                               'provider_login': adapter.login_recipe_id == recipe_id}}


def _validate_setup_values(contract, values):
    from jsonschema import Draft202012Validator, FormatChecker, validators
    # JSON Schema accepts 1.0 as an integer; setup preserves exact configuration types.
    validator = validators.extend(Draft202012Validator, type_checker=
        Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda checker, value: type(value) is int))
    schema = contract['input_schema']
    if not isinstance(values, dict):
        return {'valid': False, 'complete': False, 'missing_fields': [],
                'issues': [{'field': '', 'code': 'INVALID_TYPE'}]}
    try:
        _no_secrets(values)
    except StoreError:
        return {'valid': False, 'complete': False, 'missing_fields': [],
                'issues': [{'field': '', 'code': 'SECRET_NOT_ALLOWED'}]}
    issues = set()
    for error in validator(schema, format_checker=FormatChecker()).iter_errors(values):
        if error.validator == 'required' and not error.path:
            continue
        field = next(iter(error.path), '')
        field = field if field in schema['properties'] else ''
        code = ('UNKNOWN_FIELD' if error.validator == 'additionalProperties' else
                'INVALID_TYPE' if error.validator == 'type' else 'INVALID_VALUE')
        issues.add((field, code))
    missing = [name for name in schema['required'] if name not in values]
    return {'valid': not issues, 'complete': not issues and not missing,
            'missing_fields': missing,
            'issues': [{'field': field, 'code': code} for field, code in sorted(issues)]}


def validate_setup(recipe_id, path):
    """Validate even partial input without storage, credential access, or networking."""
    return _validate_setup_values(setup_schema(recipe_id), read_config(path))


def _setup_pointer(path):
    return '/' + '/'.join(str(token).replace('~', '~0').replace('/', '~1') for token in path)


def _current_setup_values(resource, contract):
    from .policy import pointer_get
    values = {}
    for name, paths in contract['configuration_paths'].items():
        path = paths[0]
        try:
            value = pointer_get(resource, _setup_pointer(path))
        except StoreError:
            continue
        if path[:2] == ['connection', 'env']:
            kind = contract['input_schema']['properties'][name].get('type')
            if kind == 'integer' and isinstance(value, str) and value.isascii() and value.isdigit():
                value = int(value)
            elif kind == 'boolean' and isinstance(value, str):
                if value.lower() in {'true', '1', 't'}: value = True
                elif value.lower() in {'false', '0', 'f'}: value = False
        values[name] = copy.deepcopy(value)
    return values


def configure_resource(store, recipe_id, profile_name, instance_id, path, *, expected_revision):
    """Apply explicit answers to one recipe resource in an unchanged profile revision."""
    from .policy import pointer_set
    contract = setup_schema(recipe_id)
    supplied = read_config(path)
    _require(_validate_setup_values(contract, supplied)['valid'], 'Setup answers are invalid.')
    _require(type(expected_revision) is int, 'An inspected profile revision is required.')
    template = profile_template(store, recipe_id, profile_name, instance_id)['resources'][0]
    with store.transaction():
        profile = store.get_profile(profile_name)
        if profile['revision'] != expected_revision:
            raise StoreError('REVISION_CONFLICT', 'Inspect the changed profile before applying setup answers.')
        resources = copy.deepcopy(profile['resources'])
        existing = next((resource for resource in resources if resource['instance_id'] == instance_id), None)
        if existing is not None:
            _require(get_adapter(existing['adapter']).adapter_id == contract['adapter'],
                     'An existing resource cannot be replaced with a different adapter.')
            _require(existing['connection']['transport'] == 'stdio'
                     and existing['connection']['command'] == template['connection']['command']
                     and existing['connection'].get('args', []) == template['connection']['args']
                     and [(c['capability_id'], c['tool_name']) for c in existing['capabilities']]
                     == [(c['capability_id'], c['tool_name']) for c in template['capabilities']],
                     'Use configuration import for connections outside this preparation recipe.')
        values = _current_setup_values(existing, contract) if existing is not None else {}
        values.update(supplied)
        _require(_validate_setup_values(contract, values)['complete'], 'Required setup answers are missing or invalid.')
        resource = existing if existing is not None else template
        for name, value in supplied.items():
            for destination in contract['configuration_paths'][name]:
                bound = str(value).lower() if type(value) is bool else str(value)
                pointer_set(resource, _setup_pointer(destination),
                            bound if destination[:2] == ['connection', 'env'] else value)
        if existing is None:
            resources.append(resource)
        if resources == profile['resources']:
            return profile
        config = {key: copy.deepcopy(profile[key]) for key in PROFILE_FIELDS}
        config['resources'] = resources
        _no_secrets(config)
        return store.put_profile(**config)


def _process_environment():
    excluded = {'VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', '__PYVENV_LAUNCHER__',
                '_OLD_VIRTUAL_PATH', '_OLD_VIRTUAL_PYTHONHOME', 'PYLAUNCHER_DEBUG'}
    return {key: value for key, value in os.environ.items()
            if key.upper() not in excluded
            and not key.upper().startswith(('PYTHON', '_PYTHON', 'UV_', 'CONDA_'))}


def _private_uv():
    executable = Path(sys.prefix).parent / '.runtime' / 'uv.exe'
    if not executable.is_file() or executable.is_symlink() or executable.parent.is_symlink():
        raise StoreError('CONNECTION_NOT_READY', 'Run DAERI setup to prepare its private installation tool.')
    return str(executable)


def _run(arguments, **kwargs):
    kwargs.setdefault('env', _process_environment())
    try:
        return subprocess.run(arguments, check=True, text=True, capture_output=True, timeout=300, **kwargs)
    except (OSError, subprocess.SubprocessError):
        raise StoreError('CONNECTION_NOT_READY', 'Resource preparation failed or timed out; the previous installation was preserved.') from None


def _verify(environment, item):
    python = _program(environment, 'python')
    executable = _program(environment, item['executable'])
    _require(python.is_file() and executable.is_file(), 'The prepared resource executable is missing.')
    result = _run([str(python), '-I', '-c', 'import importlib.metadata,sys; print(importlib.metadata.version(sys.argv[1]))', item['package']])
    _require(result.stdout.strip() == item['version'], 'The resource package version differs from its pinned recipe.')
    return executable


def prepare_resource(store, profile_name, instance_id, recipe_id):
    profile = store.get_profile(profile_name)
    with store.profile_operation(profile['profile_id']):
        return _prepare_resource(store, profile_name, instance_id, recipe_id)


def _prepare_resource(store, profile_name, instance_id, recipe_id):
    item, project, lock_hash = _recipe(recipe_id)
    profile = store.get_profile(profile_name)
    resource = next((value for value in profile['resources'] if value['instance_id'] == instance_id), None)
    _require(resource is not None, 'Select an instance registered in the requested profile.')
    connection = resource['connection']
    expected = prepared_executable(store, recipe_id)
    _require(get_adapter(resource['adapter']).adapter_id == get_adapter(item['adapter']).adapter_id and
             resource['type'] == item['type'] and
             connection['transport'] == 'stdio' and connection['command'] == str(expected) and
             connection.get('args', []) == item['args'], 'The selected resource does not use this preparation recipe.')
    configurable_env = {path[2] for field in get_adapter(item['adapter']).setup_fields.values()
                        for path in field['paths'] if len(path) == 3 and path[:2] == ['connection', 'env']}
    _require(all(connection.get('env', {}).get(key) == value for key, value in item['env'].items()
                 if value and key not in configurable_env),
             'The recipe requires its fixed read-only and connection-safety settings.')
    destination = _tool_directory(store, recipe_id)
    marker = destination / 'prepared.json'
    if marker.is_file():
        try: prepared = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError): prepared = {}
        _require(prepared == {'recipe': recipe_id, 'lock_sha256': lock_hash, 'version': item['version']},
                 'Prepared program metadata does not match the selected recipe.')
        executable = _verify(destination / '.venv', item)
        return {'recipe': recipe_id, 'instance_id': instance_id, 'ready': True, 'reused': True,
                'version': item['version'], 'command': str(executable)}
    uv = _private_uv()
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require(not parent.is_symlink() and not destination.is_symlink(), 'The managed tools location is invalid.')
    lock = parent / '.prepare.lock'
    try: lock.mkdir(mode=0o700)
    except FileExistsError:
        raise StoreError('CONNECTION_NOT_READY', 'Resource preparation is already running or was interrupted; inspect its lock before retrying.') from None
    candidate = parent / ('.prepare.' + uuid4().hex)
    try:
        candidate.mkdir(mode=0o700)
        environment = candidate / '.venv'
        runtime = store.data_dir / 'runtime'
        env = _process_environment() | {
            'UV_PROJECT_ENVIRONMENT': str(environment),
            'UV_PYTHON_INSTALL_DIR': str(runtime / 'python'),
            'UV_PYTHON_INSTALL_BIN': '0', 'UV_PYTHON_INSTALL_REGISTRY': '0',
            'UV_CACHE_DIR': str(runtime / 'cache'),
        }
        _run([uv, 'venv', '--relocatable', '--managed-python', '--no-config',
              '--project', str(project), str(environment)], env=env)
        _run([uv, 'sync', '--frozen', '--no-dev', '--no-editable', '--managed-python',
              '--no-config', '--project', str(project)], env=env)
        _verify(environment, item)
        (candidate / 'prepared.json').write_text(json.dumps({'recipe': recipe_id, 'lock_sha256': lock_hash, 'version': item['version']}), encoding='utf-8')
        if destination.exists():
            raise StoreError('CONNECTION_NOT_READY', 'An incomplete resource installation requires inspection before retrying.')
        candidate.replace(destination)
        executable = _verify(destination / '.venv', item)
        return {'recipe': recipe_id, 'instance_id': instance_id, 'ready': True, 'reused': False,
                'version': item['version'], 'command': str(executable)}
    finally:
        if candidate.exists(): shutil.rmtree(candidate)
        lock.rmdir()


def login_resource(store, profile_name, instance_id, *, start_url=None, sso_region=None,
                   role=None, no_browser=False, emit=None):
    profile = store.get_profile(profile_name)
    with store.profile_operation(profile['profile_id']):
        return _login_resource(store, profile_name, instance_id, start_url=start_url,
                               sso_region=sso_region, role=role, no_browser=no_browser, emit=emit)


def _login_resource(store, profile_name, instance_id, *, start_url=None, sso_region=None,
                    role=None, no_browser=False, emit=None):
    """Run explicit provider authorization in its installed SDK; forward public prompts only."""
    import queue
    import threading
    import time
    profile = store.get_profile(profile_name)
    resource = next((value for value in profile['resources'] if value['instance_id'] == instance_id), None)
    _require(resource is not None, 'Select an instance registered in the requested profile.')
    adapter = get_adapter(resource['adapter'])
    recipe_id, helper = adapter.login_recipe_id, adapter.login_helper
    _require(isinstance(recipe_id, str) and isinstance(helper, str) and helper.endswith('.py') and
             '/' not in helper and '\\' not in helper, 'This resource does not provide the selected login flow.')
    item, _, _ = _recipe(recipe_id)
    _require(get_adapter(item['adapter']).adapter_id == adapter.adapter_id and
             resource['type'] == item['type'], 'Use the prepared provider recipe for this login flow.')
    environment = _tool_directory(store, recipe_id) / '.venv'
    expected = _program(environment, item['executable'])
    connection = resource['connection']
    _require(connection['transport'] == 'stdio' and connection['command'] == str(expected.resolve()) and
             connection.get('args', []) == item['args'], 'Use the prepared provider recipe for this login flow.')
    _verify(environment, item)
    request = adapter.login_request(resource, start_url=start_url, sso_region=sso_region,
                                    role=role, no_browser=no_browser)
    _no_secrets(request)
    if emit is None:
        emit = lambda value: print(json.dumps({'status': 'pending', 'data': value, 'error': None}), flush=True)
    process = subprocess.Popen([str(_program(environment, 'python')), '-I', str(Path(__file__).with_name(helper))],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, env=_process_environment())
    events = queue.Queue()
    def read():
        try:
            for line in iter(lambda: process.stdout.readline(16385), ''):
                events.put(line)
        finally:
            events.put(None)
    reader = threading.Thread(target=read, daemon=True)
    try:
        process.stdin.write(json.dumps(request))
        process.stdin.close()
        reader.start()
        deadline = time.monotonic() + 750
        result = None
        while True:
            line = events.get(timeout=max(0, deadline - time.monotonic()))
            if line is None: break
            _require(len(line) <= 16384, 'Provider login returned an invalid response.')
            value = json.loads(line)
            if value.get('status') == 'pending':
                data = value['data']
                adapter.validate_login_prompt(data, resource)
                emit(data)
            elif value.get('status') == 'ok':
                data = value['data']
                adapter.validate_login_result(data, resource)
                result = data
            else:
                raise StoreError('AUTHENTICATION_REQUIRED', 'The provider requires authentication or rejected the device authorization.')
        process.wait(timeout=5)
        if process.returncode != 0 or result is None:
            raise StoreError('AUTHENTICATION_REQUIRED', 'Provider authentication could not be completed.')
        return result
    except (OSError, ValueError, KeyError, queue.Empty, subprocess.SubprocessError):
        raise StoreError('AUTHENTICATION_REQUIRED', 'Provider authentication failed or timed out; retry explicitly.') from None
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
        if not process.stdin.closed: process.stdin.close()
        if reader.is_alive(): reader.join(timeout=1)
