"""Explicit local configuration and pinned lower-MCP preparation; no implicit login."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
from uuid import uuid4

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


def load_catalog():
    return json.loads(Path(__file__).with_name('catalog.json').read_text(encoding='utf-8'))


def _recipe(recipe_id):
    catalog = load_catalog()
    _require(recipe_id in catalog, 'Select a registered preparation recipe.')
    bundled = Path(__file__).with_name('recipes')
    root = bundled if bundled.is_dir() else Path(__file__).resolve().parents[2] / 'recipes'
    project = root / recipe_id
    try:
        metadata = tomllib.loads((project / 'pyproject.toml').read_text(encoding='utf-8'))
        lock = (project / 'uv.lock').read_bytes()
    except (OSError, ValueError):
        raise StoreError('CONNECTION_NOT_READY', 'The pinned resource preparation recipe is unavailable.') from None
    item = catalog[recipe_id]
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
    targets = {
        'DB': {'host': 'SET-ME', 'port': 5432, 'database': 'SET-ME', 'username': 'SET-ME'},
        'Cache': {'host': 'SET-ME', 'port': 6379, 'db': 0},
        'Infra': {'profile': 'SET-ME', 'region': 'SET-ME', 'account': 'SET-ME'},
        'Source': {'server': 'https://SET-ME.invalid', 'project': 'SET-ME'},
        'Document': {'server': 'https://SET-ME.invalid', 'space': 'SET-ME'},
    }
    resource = {'instance_id': instance_id, 'type': item['type'], 'adapter': item['adapter'],
                'target': targets[item['type']], 'identity_ref': 'identity:' + profile_name + ':' + instance_id,
                'connection': {'transport': 'stdio', 'command': str(prepared_executable(store, recipe_id)),
                               'args': copy.deepcopy(item['args']), 'env': copy.deepcopy(item['env']),
                               'auth_env': {name: 'credential:' + profile_name + '.' + instance_id + '.' + name.lower()
                                            for name in item['auth_env']}},
                'capabilities': copy.deepcopy(item['capabilities']), 'policy': {}}
    return {'name': profile_name, 'core_context': [], 'resources': [resource],
            'retention_days': None, 'long_term_memory_days': None, 'memory_policy': {}}


def _run(arguments, **kwargs):
    try:
        return subprocess.run(arguments, check=True, text=True, capture_output=True, timeout=300, **kwargs)
    except (OSError, subprocess.SubprocessError):
        raise StoreError('CONNECTION_NOT_READY', 'Resource preparation failed or timed out; the previous installation was preserved.') from None


def _verify(environment, item):
    python = _program(environment, 'python')
    executable = _program(environment, item['executable'])
    _require(python.is_file() and executable.is_file(), 'The prepared resource executable is missing.')
    result = _run([str(python), '-c', 'import importlib.metadata,sys; print(importlib.metadata.version(sys.argv[1]))', item['package']])
    _require(result.stdout.strip() == item['version'], 'The resource package version differs from its pinned recipe.')
    return executable


def prepare_resource(store, profile_name, instance_id, recipe_id):
    item, project, lock_hash = _recipe(recipe_id)
    profile = store.get_profile(profile_name)
    resource = next((value for value in profile['resources'] if value['instance_id'] == instance_id), None)
    _require(resource is not None, 'Select an instance registered in the requested profile.')
    connection = resource['connection']
    expected = prepared_executable(store, recipe_id)
    _require(resource['adapter'] == item['adapter'] and resource['type'] == item['type'] and
             connection['transport'] == 'stdio' and connection['command'] == str(expected) and
             connection.get('args', []) == item['args'], 'The selected resource does not use this preparation recipe.')
    _require(all(connection.get('env', {}).get(key) == value for key, value in item['env'].items() if value),
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
    uv = shutil.which('uv')
    if not uv:
        raise StoreError('CONNECTION_NOT_READY', 'Run DAERI environment preparation so uv is available in the host PATH.')
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
        _run([uv, 'venv', '--relocatable', '--project', str(project), str(environment)])
        env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(environment))
        _run([uv, 'sync', '--frozen', '--no-dev', '--no-editable', '--project', str(project)], env=env)
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
    """Run explicit provider authorization in its installed SDK; forward public prompts only."""
    import queue
    import threading
    import time
    profile = store.get_profile(profile_name)
    resource = next((value for value in profile['resources'] if value['instance_id'] == instance_id), None)
    _require(resource is not None and resource['adapter'] == 'aws_read', 'This resource does not provide the selected login flow.')
    item, _, _ = _recipe('infra-aws')
    environment = _tool_directory(store, 'infra-aws') / '.venv'
    expected = _program(environment, item['executable'])
    connection = resource['connection']
    _require(connection['transport'] == 'stdio' and connection['command'] == str(expected.resolve()) and
             connection.get('args', []) == item['args'], 'Use the prepared provider recipe for this login flow.')
    _verify(environment, item)
    target = resource['target']
    _require(connection.get('env', {}).get('AWS_PROFILE') == target.get('profile') and
             connection.get('env', {}).get('AWS_REGION') == target.get('region'), 'The selected provider profile differs from the resource target.')
    request = {'profile': target.get('profile'), 'account': target.get('account'), 'region': target.get('region'),
               'start_url': start_url, 'sso_region': sso_region, 'role': role, 'no_browser': no_browser}
    _no_secrets(request)
    if emit is None:
        emit = lambda value: print(json.dumps({'status': 'pending', 'data': value, 'error': None}), flush=True)
    process = subprocess.Popen([str(_program(environment, 'python')), str(Path(__file__).with_name('provider_login.py'))],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
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
                _require(set(data) == {'provider', 'verification_uri', 'user_code', 'provider_profile'} and
                         data['provider_profile'] == target['profile'], 'Provider login returned an invalid prompt.')
                emit(data)
            elif value.get('status') == 'ok':
                data = value['data']
                _require(set(data) == {'authenticated', 'provider_profile', 'account', 'role', 'expires_at'} and
                         data['authenticated'] is True and data['provider_profile'] == target['profile'] and
                         data['account'] == target['account'], 'Provider login returned an invalid completion.')
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
