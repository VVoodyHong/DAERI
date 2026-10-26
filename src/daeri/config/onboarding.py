"""Selected-resource YAML drafts, local validation and atomic configuration."""

import copy
import hashlib
import hmac
import json
import os
import re
import stat
from contextlib import contextmanager
from uuid import uuid4

import yaml

from daeri.infrastructure.credentials import Credentials, resource_reference
from daeri.infrastructure.embedding import (
    api_adapter_catalog,
    api_embedding_draft,
    embedding_field_comments,
    embedding_credential_fields,
    validate_embedding_config,
)
from daeri.core.security import redact
from daeri.config.setup import (
    PROFILE_FIELDS,
    REFERENCE,
    _current_setup_values,
    _no_secrets,
    _require,
    _validate_setup_values,
    build_setup_resource,
    load_catalog,
    profile_template,
    read_config,
    resource_credential_fields,
    setup_schema,
)
from daeri.config.setup_yaml import parse_yaml, write_yaml
from daeri.core.errors import StoreError
from daeri.core.policy import _profile_memory_policy, retention_deadline


def _instance_prefix(resource_type):
    return re.sub(r'[^a-z0-9]+', '-', resource_type.lower()).strip('-')[:64].rstrip('-') or 'resource'


def selection_catalog():
    labels = {'DB': '데이터베이스', 'Cache': '캐시', 'Infra': '인프라',
              'Source': '소스 코드', 'Document': '문서', 'Issue': '이슈'}
    groups = {}
    for recipe, item in load_catalog().items():
        group = groups.setdefault(item['type'], {
            'type': item['type'], 'label': labels.get(item['type'], item['type']),
            'instance_prefix': _instance_prefix(item['type']), 'products': []})
        group['products'].append({'recipe_id': recipe, 'display_name': item['display_name']})
    for group in groups.values():
        group['description'] = '사용 가능한 제품: ' + ', '.join(item['display_name'] for item in group['products'])
    return {'groups': list(groups.values()), 'embedding_adapters': api_adapter_catalog()}


def _paths(store, name):
    _require(isinstance(name, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)),
             'profile을 확인하세요.')
    path = store.data_dir / 'config' / 'profiles' / (name + '.yaml')
    metadata = store.data_dir / 'setup' / name / 'draft.json'
    legacy = metadata.with_name('setup.yaml')
    try:
        for directory in _directories(store, path, metadata):
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                continue
            _require(stat.S_ISDIR(mode), '설정 디렉터리 경로를 확인하세요.')
        for file in (path, metadata, legacy):
            try:
                mode = file.lstat().st_mode
            except FileNotFoundError:
                continue
            _require(stat.S_ISREG(mode), '설정 파일 형식을 확인하세요.')
    except OSError:
        raise StoreError('STORE_UNAVAILABLE', '설정 파일 접근 권한과 저장소 상태를 확인하세요.') from None
    return path, metadata


def _directories(store, path, metadata):
    return (store.data_dir, path.parent.parent, path.parent, metadata.parent.parent, metadata.parent)


def _active_paths(store, name):
    path, metadata = _paths(store, name)
    legacy = metadata.with_name('setup.yaml')
    try:
        present, old, managed = path.exists(), legacy.exists(), metadata.exists()
    except OSError:
        raise StoreError('STORE_UNAVAILABLE', '설정 파일 접근 권한과 저장소 상태를 확인하세요.') from None
    _require(not (present and old), '사용자 설정 YAML을 하나로 복구하세요.')
    _require((present or old) == managed, '설정 YAML과 초안 관리 정보를 함께 복구하세요.')
    return (legacy if old else path), metadata, managed


def draft_status(store, name):
    """Inspect draft file availability before creation, preserving unfinished edits."""
    result = {'profile': name, 'state': 'new', 'path': None}
    try:
        path, metadata, present = _active_paths(store, name)
        if present:
            for file in (path, metadata):
                with file.open('rb') as stream:
                    stream.read(1)
    except OSError:
        raise StoreError('STORE_UNAVAILABLE', '초안 파일 접근 권한과 저장소 상태를 확인하세요.') from None
    return {'profile': name, 'state': 'resume', 'path': str(path)} if present else result


def _prepare_directories(store, path, metadata):
    try:
        for directory in _directories(store, path, metadata):
            directory.mkdir(exist_ok=True, mode=0o700)
            _require(stat.S_ISDIR(directory.lstat().st_mode), '설정 디렉터리 경로를 확인하세요.')
            if os.name != 'nt':
                directory.chmod(0o700)
    except OSError:
        raise StoreError('STORE_UNAVAILABLE', '설정 디렉터리를 준비하지 못했습니다.') from None


def _migrate_legacy(legacy, path):
    linked = False
    try:
        # A no-overwrite hard link keeps the exact edited file during relocation.
        os.link(legacy, path, follow_symlinks=False)
        linked = True
        if os.name != 'nt':
            path.chmod(0o600)
        legacy.unlink()
    except OSError:
        if linked:
            try:
                path.unlink()
            except OSError:
                pass
        raise StoreError('STORE_UNAVAILABLE', '설정 YAML 이관 상태를 확인하고 다시 시도하세요.') from None


def _write(path, text):
    temporary = path.with_name('.' + uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError:
        raise StoreError('STORE_UNAVAILABLE', '설정 초안을 저장하지 못했습니다.') from None
    finally:
        temporary.unlink(missing_ok=True)


def _current(store, name):
    try:
        return store.get_profile(name)
    except StoreError as error:
        if error.code != 'NOT_FOUND':
            raise
        return None


def _base(profile):
    return {key: profile[key] for key in ('profile_id', 'revision')} if profile else None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _schema_digest(recipe):
    return _digest([load_catalog()[recipe], setup_schema(recipe)])


def _read_draft(store, name):
    path, metadata, _ = _active_paths(store, name)
    meta = read_config(metadata)
    _require(isinstance(meta, dict) and set(meta) == {'profile', 'base', 'schemas'}
             and meta['profile'] == name and isinstance(meta['schemas'], dict),
             '초안 관리 정보를 확인하세요.')
    try:
        with path.open('rb') as stream:
            content = stream.read(2 * 1024 * 1024 + 1)
    except OSError:
        raise StoreError('INVALID_INPUT', '설정 YAML 파일을 읽을 수 없습니다.') from None
    _require(len(content) <= 2 * 1024 * 1024, '설정 파일의 크기 제한을 초과했습니다.')
    document = parse_yaml(content)
    public = copy.deepcopy(document)
    for block in public.get('resources', {}).values() if isinstance(public.get('resources'), dict) else ():
        if isinstance(block, dict):
            block.pop('credentials', None)
    if isinstance(public.get('embedding'), dict):
        public['embedding'].pop('credentials', None)
    _no_secrets(public)
    # Parsed scalar values have their own schema; retain the comment credential check.
    text = content.decode('utf-8')
    comments, cursor = [], 0
    for token in yaml.scan(text):
        if isinstance(token, yaml.tokens.ScalarToken):
            comments.append(text[cursor:token.start_mark.index])
            comments.append(' ' * (token.end_mark.index - token.start_mark.index))
            cursor = token.end_mark.index
    comments.append(text[cursor:])
    comments = ''.join(comments)
    _require(redact(comments) == comments, '비밀값은 해당 연결의 credentials 항목에 입력하세요.')
    _require(document.get('profile') == name, '요청한 profile과 YAML의 profile이 다릅니다.')
    _require(set(document) <= (PROFILE_FIELDS - {'name'}) | {'profile'}, '알 수 없는 설정 항목이 있습니다.')
    _require(isinstance(document.get('resources'), dict), 'resources는 이름별 설정 객체여야 합니다.')
    return path, metadata, meta, document, hashlib.sha256(content).hexdigest()


def _embedding_draft(name, provider, adapter=None):
    _require(isinstance(provider, str) and provider in {'local', 'api', 'openai_compatible', 'keyword_only'},
             '기억 검색 방식을 확인하세요.')
    if provider == 'openai_compatible':
        _require(adapter is None or adapter == 'openai_compatible', '회사 API 연결 방식을 확인하세요.')
        provider, adapter = 'api', 'openai_compatible'
    if provider == 'api':
        return api_embedding_draft(name, adapter)
    _require(adapter is None, '회사 API 연결 방식은 회사 임베딩 API와 함께 선택하세요.')
    return {'provider': provider}


def _embedding_identity(value):
    if not isinstance(value, dict):
        return None
    provider = value.get('provider')
    return ('api', 'openai_compatible') if provider == 'openai_compatible' else (provider, value.get('adapter'))


def _resource_block(store, name, instance, recipe, profile):
    contract = setup_schema(recipe)
    existing = next((r for r in profile['resources'] if r['instance_id'] == instance), None) if profile else None
    values = {}
    if existing:
        # Reuse the common compatibility check rather than rewriting a manual connection.
        build_setup_resource(store, recipe, name, instance, {}, existing=existing)
        values = _current_setup_values(existing, contract)
    for field, schema in contract['input_schema']['properties'].items():
        if field not in values and ('default' in schema or field in contract['input_schema']['required']):
            values[field] = copy.deepcopy(schema.get('default'))
    return {'adapter': recipe, 'inputs': values}


def _render(document, credential_fields=None):
    text = '# 접속 정보와 credentials의 비밀번호·토큰을 입력하세요.\n'
    text += '# 적용하면 비밀값을 인증 저장소에 저장하고 이 파일에는 참조를 남깁니다.\n'
    text += '# 빈 필수값을 채운 뒤 DAERI에 검증과 적용을 요청하세요.\n'
    text += write_yaml(document)
    # Keep field guidance beside the values users edit, using each adapter's metadata.
    lines, inputs, section, instance, resource_section = [], None, None, None, None
    credential_fields = credential_fields or {}
    embedding_comments = embedding_field_comments(document.get('embedding'))
    embedding_credentials = embedding_credential_fields(document.get('embedding'))
    if embedding_credentials:
        embedding_comments.setdefault('credentials', '인증값을 입력하거나 저장한 인증 참조를 사용합니다.')
    for line in text.splitlines():
        if line and not line.startswith(' '):
            inputs, section, instance, resource_section = None, line, None, None
        if section == 'resources:' and line.startswith('  ') and not line.startswith('    '):
            instance = line.strip().removesuffix(':')
        if section == 'resources:' and line.startswith('    ') and not line.startswith('      '):
            resource_section = line.strip().split(':', 1)[0]
        if section == 'resources:' and line.startswith('    adapter: '):
            recipe = line.split(': ', 1)[1]
            inputs = setup_schema(recipe)['input_schema']['properties']
        if section == 'embedding:' and line.startswith('  ') and not line.startswith('    '):
            key = line.strip().split(':', 1)[0]
            for comment in embedding_comments.get(key, '').splitlines():
                lines.append('  # ' + comment)
        if inputs and resource_section == 'inputs' and line.startswith('      ') and not line.startswith('        '):
            key = line.strip().split(':', 1)[0]
            if key in inputs:
                schema = inputs[key]
                hint = schema.get('description') or schema.get('title', key)
                example = schema.get('examples', [])
                suffix = ' 예: ' + json.dumps(example[0], ensure_ascii=False) if example else ''
                lines.append('      # ' + ' '.join(hint.splitlines()) + suffix)
        if resource_section == 'credentials' and line.startswith('      ') and not line.startswith('        '):
            key = line.strip().split(':', 1)[0]
            field = credential_fields.get(instance, {}).get(key)
            if field:
                lines.append('      # ' + field['description'])
        if section == 'embedding:' and line.startswith('    ') and not line.startswith('      '):
            key = line.strip().split(':', 1)[0]
            field = embedding_credentials.get(key)
            if field:
                lines.append('    # ' + field['description'])
        lines.append(line)
    return '\n'.join(lines) + '\n'


def _credential_slots(store, name, document, profile):
    fields, changed = {}, False
    existing = {resource['instance_id']: resource for resource in profile['resources']} if profile else {}
    for instance, block in document['resources'].items():
        if (not isinstance(block, dict) or not isinstance(block.get('adapter'), str)
                or block['adapter'] not in load_catalog()):
            continue
        resource = existing.get(instance) or profile_template(store, block['adapter'], name, instance)['resources'][0]
        fields[instance] = resource_credential_fields(resource)
        if fields[instance] and 'credentials' not in block:
            block['credentials'] = {field: {'ref': spec['reference']} if instance in existing else None
                                    for field, spec in fields[instance].items()}
            changed = True
    embedding = document.get('embedding')
    selected = embedding_credential_fields(embedding)
    if selected and 'credentials' not in embedding:
        previous = embedding_credential_fields(profile['embedding']) if profile else {}
        embedding['credentials'] = {field: {'ref': spec['reference']} if field in previous else None
                                    for field, spec in selected.items()}
        changed = True
    return fields, changed


def _automatic_resources(store, name, selections, recipes, blocks, profile):
    selected = selections.copy()
    if not recipes:
        return selected
    catalog = load_catalog()
    existing = {resource['instance_id']: resource for resource in profile['resources']} if profile else {}
    occupied = set(existing) | set(blocks)
    for recipe in recipes:
        _require(isinstance(recipe, str) and recipe in catalog, '연결할 제품을 확인하세요.')
        item = catalog[recipe]
        prefix, number = _instance_prefix(item['type']), 1
        instance = prefix
        while instance in selected or instance in occupied:
            if instance not in selected:
                if instance in blocks:
                    block = blocks[instance]
                    if isinstance(block, dict) and block.get('adapter') == recipe:
                        break
                else:
                    try:
                        build_setup_resource(store, recipe, name, instance, {}, existing=existing[instance])
                    except StoreError:
                        pass
                    else:
                        break
            number += 1
            instance = prefix + '-' + str(number)
        selected[instance] = recipe
    return selected


def create_draft(store, name, selections=None, *, recipes=None, embedding=None, embedding_adapter=None, refresh=False):
    """Preserve edited values; refresh a stale base only when explicitly requested."""
    path, metadata = _paths(store, name)
    selections = {} if selections is None else selections
    _require(isinstance(selections, dict), '연결할 리소스를 확인하세요.')
    recipes = [] if recipes is None else recipes
    _require(isinstance(recipes, list), '연결할 제품 목록을 확인하세요.')
    _require(embedding is not None or embedding_adapter is None,
             '회사 API 연결 방식은 기억 검색 방식과 함께 선택하세요.')
    requested_embedding = _embedding_draft(name, embedding, embedding_adapter) if embedding is not None else None
    with store.transaction():
        profile = _current(store, name)
        active, metadata, exists = _active_paths(store, name)
        if exists:
            _, _, meta, document, _ = _read_draft(store, name)
        else:
            document = {'profile': name, 'resources': {}}
            meta = {'profile': name, 'base': _base(profile), 'schemas': {}}
        selections = _automatic_resources(store, name, selections, recipes, document['resources'], profile)
        changed = not exists
        for instance, recipe in selections.items():
            _require(isinstance(instance, str) and isinstance(recipe, str), '연결할 리소스 이름과 제품을 확인하세요.')
            # profile_template validates names and the installed recipe contract.
            profile_template(store, recipe, name, instance)
            block = document['resources'].get(instance)
            if instance in document['resources']:
                _require(isinstance(block, dict) and block.get('adapter') == recipe,
                         '기존 연결을 다른 제품으로 바꿀 수 없습니다.')
            else:
                document['resources'][instance] = _resource_block(store, name, instance, recipe, profile)
                changed = True
            if recipe not in meta['schemas']:
                meta['schemas'][recipe] = _schema_digest(recipe)
        if embedding is not None:
            if 'embedding' not in document:
                previous = profile['embedding'] if profile else None
                if _embedding_identity(previous) == _embedding_identity(requested_embedding):
                    selected = {key: value for key, value in previous.items() if key != 'allow_remote'}
                    document['embedding'] = validate_embedding_config(selected)
                else:
                    document['embedding'] = requested_embedding
                changed = True
            elif (document['embedding'] == {'provider': 'api', 'adapter': None}
                    and requested_embedding.get('provider') == 'api'
                    and requested_embedding.get('adapter') is not None):
                document['embedding'] = requested_embedding
                changed = True
            else:
                _require(_embedding_identity(document['embedding']) == _embedding_identity(requested_embedding),
                         '기존 초안의 기억 검색 방식은 YAML에서 수정하세요.')
                if requested_embedding['provider'] == 'api' and 'allow_remote' in document['embedding']:
                    document['embedding'].pop('allow_remote')
                    changed = True
        elif not exists and not profile:
            document['embedding'] = _embedding_draft(name, 'local')
        credential_fields, added_credentials = _credential_slots(store, name, document, profile)
        changed = changed or added_credentials
        if refresh:
            meta['base'] = _base(profile)
            _require(all(isinstance(block, dict) and isinstance(block.get('adapter'), str)
                         and block['adapter'] in load_catalog() for block in document['resources'].values()),
                     '리소스별 제품 설정을 확인하세요.')
            meta['schemas'] = {block['adapter']: _schema_digest(block['adapter'])
                               for block in document['resources'].values()}
        _prepare_directories(store, path, metadata)
        if exists and active != path:
            _migrate_legacy(active, path)
        if changed:
            _write(path, _render(document, credential_fields))
        _write(metadata, json.dumps(meta, ensure_ascii=False, allow_nan=False))
        return {'profile': name, 'path': str(path), 'existing': exists,
                'base_revision': meta['base']['revision'] if meta['base'] else None,
                'resources': list(document['resources'])}


def _issue(field, code='INVALID_INPUT', message='입력값과 허용 범위를 확인하세요.'):
    return {'field': field, 'code': code, 'message': message}


def _credential_values(document):
    values = {}
    blocks = [('resources.' + instance, block) for instance, block in document.get('resources', {}).items()]
    blocks.append(('embedding', document.get('embedding', {})))
    for prefix, block in blocks:
        supplied = block.get('credentials', {}) if isinstance(block, dict) else {}
        if isinstance(supplied, dict):
            values.update({prefix + '.credentials.' + key: value for key, value in supplied.items()})
    return values


def _collect_credentials(values, fields, prefix, updates, issues):
    if not isinstance(values, dict) or not set(values) <= set(fields):
        issues.append(_issue(prefix, message='선택한 연결의 인증 항목을 확인하세요.'))
        return {}
    sanitized = {}
    for key, field in fields.items():
        value = values.get(key)
        reference = field['reference']
        path = prefix + '.' + key
        if isinstance(value, dict) and set(value) == {'ref'} and isinstance(value['ref'], str) and REFERENCE.fullmatch(value['ref']):
            reference = value['ref']
        elif value is not None:
            if (not isinstance(value, str) or not 1 <= len(value) <= 16384
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)
                    or not isinstance(reference, str) or not reference.startswith('credential:')
                    or not REFERENCE.fullmatch(reference)):
                issues.append(_issue(path, message='인증값은 한 줄 문자열, null 또는 유효한 ref 객체로 입력하세요.'))
                continue
            try:
                value.encode('utf-8')
            except UnicodeError:
                issues.append(_issue(path, message='UTF-8 인증값을 입력하세요.'))
                continue
            if reference in updates and updates[reference] != value:
                issues.append(_issue(path, message='같은 인증 참조에는 동일한 값을 입력하세요.'))
                continue
            updates[reference] = value
        if not isinstance(reference, str) or not REFERENCE.fullmatch(reference):
            issues.append(_issue(path, message='유효한 인증 참조를 확인하세요.'))
            continue
        sanitized[key] = {'ref': reference}
    return sanitized


def _configuration(store, name, document, profile, meta):
    from daeri.core.policy import validate_resource_policy
    from daeri.infrastructure.resources import validate_resource
    config = ({key: copy.deepcopy(profile[key]) for key in PROFILE_FIELDS} if profile else
              {'name': name, 'resources': [], 'core_context': [], 'retention_days': None,
               'long_term_memory_days': None, 'memory_policy': None, 'embedding': {'provider': 'local'}})
    for key in PROFILE_FIELDS - {'name', 'resources'}:
        if key in document:
            config[key] = copy.deepcopy(document[key])
    issues, updates = [], {}
    sanitized = copy.deepcopy(document)
    for instance, block in document['resources'].items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', instance):
            issues.append(_issue('resources', message='리소스 이름을 확인하세요.'))
            continue
        field = 'resources.' + instance
        if (not isinstance(block, dict) or not {'adapter', 'inputs'} <= block.keys()
                or not set(block) <= {'adapter', 'inputs', 'policy', 'credentials'}
                or not isinstance(block['adapter'], str) or block['adapter'] not in meta['schemas']):
            issues.append(_issue(field, message='선택한 제품과 입력 항목을 확인하세요.'))
            continue
        recipe = block['adapter']
        existing = next((r for r in config['resources'] if r['instance_id'] == instance), None)
        contract = setup_schema(recipe)
        partial = _validate_setup_values(contract, block['inputs'])
        if not partial['valid']:
            for issue in partial['issues']:
                key = issue['field']
                # Error paths use validated schema field names.
                path = field + '.inputs' + ('.' + key if key in contract['input_schema']['properties'] else '')
                issues.append(_issue(path, message='접속 필수값·타입·허용 조건을 확인하세요.'))
            continue
        merged = _current_setup_values(existing, contract) if existing else {}
        merged.update(block['inputs'])
        missing = _validate_setup_values(contract, merged)['missing_fields']
        if missing:
            issues.extend(_issue(field + '.inputs.' + key, message='필수값을 입력하세요.') for key in missing)
            continue
        try:
            resource = build_setup_resource(store, recipe, name, instance, block['inputs'], existing=existing,
                                            refresh_contract=True)
            credential_fields = resource_credential_fields(resource)
            supplied = copy.deepcopy(block.get('credentials', {}))
            if existing and isinstance(supplied, dict):
                previous = resource_credential_fields(existing)
                for key, spec in credential_fields.items():
                    old = previous.get(key)
                    if (old and old['environment'] != spec['environment'] and
                            old['reference'] == resource_reference(name, instance, old['environment']) and
                            supplied.get(key) == {'ref': old['reference']}):
                        supplied[key] = {'ref': spec['reference']}
            safe = _collect_credentials(supplied, credential_fields,
                                        field + '.credentials', updates, issues)
            if safe:
                sanitized['resources'][instance]['credentials'] = safe
                for key, spec in credential_fields.items():
                    if key in safe:
                        resource['connection']['auth_env'][spec['environment']] = safe[key]['ref']
            if 'policy' in block:
                resource['policy'] = copy.deepcopy(block['policy'])
            validate_resource(resource)
            resource['policy'] = validate_resource_policy(resource.get('policy'))
            if resource['policy'] is None:
                sanitized['resources'][instance].pop('policy', None)
        except StoreError:
            issues.append(_issue(field, message='필수값·허용 정책·기존 연결 호환성을 확인하세요.'))
            continue
        if existing:
            config['resources'][config['resources'].index(existing)] = resource
        else:
            config['resources'].append(resource)
    for key in ('retention_days', 'long_term_memory_days'):
        try:
            retention_deadline(config[key])
        except StoreError:
            issues.append(_issue(key, message='계산 가능한 보존기간의 양의 정수 또는 null을 입력하세요.'))
    if not isinstance(config['core_context'], list) or any(not isinstance(v, str) for v in config['core_context']):
        issues.append(_issue('core_context'))
    try:
        _require(isinstance(config['embedding'], dict), '기억 검색 설정은 객체로 입력하세요.')
        embedding = config['embedding']
        fields = embedding_credential_fields(embedding)
        credentials = embedding.pop('credentials', {})
        safe = _collect_credentials(credentials, fields, 'embedding.credentials', updates, issues)
        for key, spec in fields.items():
            if key in safe:
                embedding[spec['config_key']] = safe[key]['ref']
        config['embedding'] = validate_embedding_config(config['embedding'])
        if 'embedding' in sanitized:
            sanitized['embedding'] = copy.deepcopy(config['embedding'])
            if safe:
                sanitized['embedding']['credentials'] = safe
    except StoreError:
        embedding = config['embedding']
        if (isinstance(embedding, dict) and embedding.get('provider') == 'api'
                and (not isinstance(embedding.get('adapter'), str)
                     or embedding['adapter'] not in {item['adapter_id'] for item in api_adapter_catalog()})):
            issues.append(_issue('embedding.adapter', message='지원하는 API 연결 방식을 선택해 주세요.'))
        else:
            issues.append(_issue('embedding', message='선택한 기억 검색 방식의 접속·인증 필수값을 확인하세요.'))
    try:
        config['memory_policy'] = _profile_memory_policy(config['memory_policy'], config['resources'])
        if config['memory_policy'] is None:
            sanitized.pop('memory_policy', None)
    except StoreError:
        issues.append(_issue('memory_policy'))
    return config, issues, sanitized, updates


def _read_contract(resource):
    return {'discover_tools': resource.get('discover_tools', False),
            'capabilities': sorted([
                {key: copy.deepcopy(capability[key]) for key in ('capability_id', 'tool_name', 'constraints')}
                for capability in resource['capabilities']], key=lambda item: item['capability_id']),
            'enabled_tools': resource['connection'].get('env', {}).get('ENABLED_TOOLS')}


def _inspect(store, name):
    path, metadata, meta, document, digest = _read_draft(store, name)
    profile = _current(store, name)
    if meta['base'] != _base(profile):
        raise StoreError('REVISION_CONFLICT', 'profile이 변경되었습니다. 변경을 확인하고 초안 기준을 갱신하세요.')
    for recipe, fingerprint in meta['schemas'].items():
        if recipe not in load_catalog() or fingerprint != _schema_digest(recipe):
            raise StoreError('REVISION_CONFLICT', '제품 입력 규격이 변경되었습니다. 초안을 확인하고 기준을 갱신하세요.')
    config, issues, sanitized, updates = _configuration(store, name, document, profile, meta)
    token = _digest([meta, digest, config]) if not issues else None
    preview = copy.deepcopy(sanitized) if not issues else None
    if preview is not None:
        for instance, block in preview['resources'].items():
            resource = next(r for r in config['resources'] if r['instance_id'] == instance)
            block['inputs'] = _current_setup_values(resource, setup_schema(block['adapter']))
            block['policy'] = copy.deepcopy(resource.get('policy'))
        preview['embedding'] = copy.deepcopy(config['embedding'])
        if sanitized.get('embedding', {}).get('credentials'):
            preview['embedding']['credentials'] = copy.deepcopy(sanitized['embedding']['credentials'])
        preview['defaults'] = {'retention_days': config['retention_days'],
                               'long_term_memory_days': config['long_term_memory_days']}
        current_resources = {r['instance_id']: r for r in profile['resources']} if profile else {}
        selected_credentials = _credential_values(sanitized)
        preview['changes'] = {'created': profile is None,
                              'updated_resources': [r['instance_id'] for r in config['resources']
                                                    if r != current_resources.get(r['instance_id'])],
                              'resource_contracts': {
                                  resource['instance_id']: {'before': _read_contract(previous),
                                                            'after': _read_contract(resource)}
                                  for resource in config['resources']
                                  if (previous := current_resources.get(resource['instance_id'])) is not None
                                  and _read_contract(previous) != _read_contract(resource)},
                              'settings': [key for key in sorted(PROFILE_FIELDS - {'name', 'resources'})
                                           if profile is None or config[key] != profile[key]],
                              'changed': bool(updates) or profile is None or any(config[key] != profile[key] for key in PROFILE_FIELDS),
                              'credentials': [path for path, value in _credential_values(document).items()
                                              if isinstance(value, str) and path in selected_credentials]}
    result = {'valid': not issues, 'complete': not issues, 'issues': issues,
              'preview': preview, 'confirmation_token': token}
    return result, config, profile, path, metadata, meta, sanitized, updates


def validate_draft(store, name):
    with store.transaction():
        try:
            return _inspect(store, name)[0]
        except StoreError as error:
            if error.code != 'INVALID_INPUT':
                raise
            return {'valid': False, 'complete': False,
                    'issues': [_issue(_validation_filename(store, name), message=error.message)],
                    'preview': None, 'confirmation_token': None}


def _validation_filename(store, name):
    try:
        return _active_paths(store, name)[0].name
    except StoreError:
        return (name + '.yaml' if isinstance(name, str)
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name) else 'profile')


def _confirm(result, confirmation):
    token = result['confirmation_token']
    if (not token or not isinstance(confirmation, str) or not re.fullmatch('[0-9a-f]{64}', confirmation) or
            not hmac.compare_digest(confirmation, token)):
        raise StoreError('REVISION_CONFLICT', '현재 YAML의 검증 결과를 확인한 뒤 적용하세요.')


def apply_draft(store, name, confirmation, *, credentials=None):
    vault = credentials if credentials is not None else Credentials()
    originals, changed_refs = {}, set()

    @contextmanager
    def apply_guard():
        inspected = _inspect(store, name)
        _confirm(inspected[0], confirmation)
        with vault.updating(inspected[-1]) as changed:
            changed_refs.update(changed)
            try:
                yield
            except BaseException:
                restored = True
                for file, original in originals.items():
                    try:
                        if not file.exists() or file.read_bytes() != original:
                            _write(file, original.decode('utf-8'))
                    except (OSError, StoreError):
                        restored = False
                if not restored:
                    raise StoreError('CREDENTIAL_UPDATE_PENDING', '설정 파일 복구를 확인한 뒤 같은 YAML로 다시 적용하세요.') from None
                raise

    with store.transaction(guard=apply_guard()):
        result, config, profile, path, metadata, meta, sanitized, _ = _inspect(store, name)
        _confirm(result, confirmation)
        originals.update({file: file.read_bytes() for file in (path, metadata)})
        if profile is None or changed_refs or any(config[key] != profile[key] for key in PROFILE_FIELDS):
            profile = store.put_profile(**config, create_only=profile is None)
        if parse_yaml(originals[path]) != sanitized:
            fields = {resource['instance_id']: resource_credential_fields(resource) for resource in config['resources']}
            _write(path, _render(sanitized, fields))
        meta['base'] = _base(profile)
        _write(metadata, json.dumps(meta, ensure_ascii=False, allow_nan=False))
    return profile
