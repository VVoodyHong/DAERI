"""Selected-resource YAML drafts, local validation and atomic configuration."""

import copy
import hashlib
import hmac
import json
import os
import re
from uuid import uuid4

from .embedding import validate_embedding_config
from .security import redact
from .setup import (PROFILE_FIELDS, _current_setup_values, _no_secrets, _require,
                    _validate_setup_values, build_setup_resource, load_catalog,
                    profile_template, read_config, setup_schema)
from .setup_yaml import parse_yaml, write_yaml
from .storage import StoreError, _profile_memory_policy


def selection_catalog():
    groups = {}
    for recipe, item in load_catalog().items():
        group = groups.setdefault(item['type'], {'type': item['type'], 'label': item['type'], 'products': []})
        group['products'].append({'recipe_id': recipe, 'display_name': item['display_name']})
    for group in groups.values():
        group['description'] = '사용 가능한 제품: ' + ', '.join(item['display_name'] for item in group['products'])
    return {'groups': list(groups.values())}


def _paths(store, name):
    _require(isinstance(name, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)),
             'profile을 확인하세요.')
    directory = store.data_dir / 'setup' / name
    for path in (store.data_dir, directory.parent, directory, directory / 'setup.yaml', directory / 'draft.json'):
        _require(not path.is_symlink(), '설정 파일 경로에 심볼릭 링크를 사용할 수 없습니다.')
    return directory / 'setup.yaml', directory / 'draft.json'


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
    path, metadata = _paths(store, name)
    meta = read_config(metadata)
    _require(isinstance(meta, dict) and set(meta) == {'profile', 'base', 'schemas'}
             and meta['profile'] == name and isinstance(meta['schemas'], dict),
             '초안 관리 정보를 확인하세요.')
    # Comments share the host-visible file, so validate their credential patterns too.
    try:
        with path.open('rb') as stream:
            content = stream.read(2 * 1024 * 1024 + 1)
    except OSError:
        raise StoreError('INVALID_INPUT', '설정 YAML 파일을 읽을 수 없습니다.') from None
    _require(len(content) <= 2 * 1024 * 1024, '설정 파일의 크기 제한을 초과했습니다.')
    try:
        _require(redact(content.decode('utf-8')) == content.decode('utf-8'),
                 '비밀값은 보안 입력 경로로 등록하고 YAML에는 접속값과 인증 참조를 사용하세요.')
    except UnicodeError:
        raise StoreError('INVALID_INPUT', 'UTF-8 YAML 파일을 사용하세요.') from None
    document = parse_yaml(content)
    _no_secrets(document)
    _require(document.get('profile') == name, '요청한 profile과 YAML의 profile이 다릅니다.')
    _require(set(document) <= (PROFILE_FIELDS - {'name'}) | {'profile'}, '알 수 없는 설정 항목이 있습니다.')
    _require(isinstance(document.get('resources'), dict), 'resources는 이름별 설정 객체여야 합니다.')
    return path, metadata, meta, document, hashlib.sha256(content).hexdigest()


def _embedding_draft(name, provider):
    _require(isinstance(provider, str) and provider in {'local', 'openai_compatible', 'keyword_only'},
             '기억 검색 방식을 확인하세요.')
    if provider == 'openai_compatible':
        return {'provider': provider, 'endpoint': None, 'model': None,
                'credential_ref': 'credential:' + name + '.embedding', 'allow_remote': False}
    return {'provider': provider}


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


def _render(document):
    text = '# 접속 정보와 공개 인증 설정을 입력하세요.\n'
    text += '# 비밀번호·토큰은 DAERI가 안내하는 터미널 숨김 입력 또는 제공자 로그인으로 등록합니다.\n'
    text += '# 빈 필수값을 채운 뒤 DAERI에 검증과 적용을 요청하세요.\n'
    text += write_yaml(document)
    # Render each selected product's schema hints next to its fields.
    lines, inputs = [], None
    for line in text.splitlines():
        if line.startswith('    adapter: '):
            recipe = line.split(': ', 1)[1]
            inputs = setup_schema(recipe)['input_schema']['properties']
        if inputs and line.startswith('      ') and not line.startswith('        '):
            key = line.strip().split(':', 1)[0]
            if key in inputs:
                schema = inputs[key]
                hint = schema.get('description') or schema.get('title', key)
                example = schema.get('examples', [])
                suffix = ' 예: ' + json.dumps(example[0], ensure_ascii=False) if example else ''
                lines.append('      # ' + ' '.join(hint.splitlines()) + suffix)
        lines.append(line)
    return '\n'.join(lines) + '\n'


def create_draft(store, name, selections=None, *, embedding=None, refresh=False):
    """Preserve edited values; refresh a stale base only when explicitly requested."""
    path, metadata = _paths(store, name)
    selections = {} if selections is None else selections
    _require(isinstance(selections, dict), '연결할 리소스를 확인하세요.')
    with store.transaction():
        profile = _current(store, name)
        exists = path.exists() or metadata.exists()
        if exists:
            _, _, meta, document, _ = _read_draft(store, name)
        else:
            document = {'profile': name, 'resources': {}}
            meta = {'profile': name, 'base': _base(profile), 'schemas': {}}
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
                document['embedding'] = (copy.deepcopy(profile['embedding']) if profile and
                    profile['embedding']['provider'] == embedding else _embedding_draft(name, embedding))
                changed = True
            else:
                _require(isinstance(embedding, str) and isinstance(document['embedding'], dict)
                         and document['embedding'].get('provider') == embedding,
                         '기존 초안의 기억 검색 방식은 YAML에서 수정하세요.')
        elif not exists and not profile:
            document['embedding'] = _embedding_draft(name, 'local')
        if refresh:
            meta['base'] = _base(profile)
            _require(all(isinstance(block, dict) and isinstance(block.get('adapter'), str)
                         and block['adapter'] in load_catalog() for block in document['resources'].values()),
                     '리소스별 제품 설정을 확인하세요.')
            meta['schemas'] = {block['adapter']: _schema_digest(block['adapter'])
                               for block in document['resources'].values()}
        if not path.parent.exists():
            path.parent.mkdir(parents=True, mode=0o700)
        if os.name != 'nt':
            path.parent.parent.chmod(0o700)
            path.parent.chmod(0o700)
        if changed:
            _write(path, _render(document))
        _write(metadata, json.dumps(meta, ensure_ascii=False, allow_nan=False))
        return {'profile': name, 'path': str(path), 'existing': exists,
                'base_revision': meta['base']['revision'] if meta['base'] else None,
                'resources': list(document['resources'])}


def _issue(field, code='INVALID_INPUT', message='입력값과 허용 범위를 확인하세요.'):
    return {'field': field, 'code': code, 'message': message}


def _configuration(store, name, document, profile, meta):
    from .policy import validate_resource_policy
    from .resources import validate_resource
    config = ({key: copy.deepcopy(profile[key]) for key in PROFILE_FIELDS} if profile else
              {'name': name, 'resources': [], 'core_context': [], 'retention_days': None,
               'long_term_memory_days': None, 'memory_policy': {}, 'embedding': {'provider': 'local'}})
    for key in PROFILE_FIELDS - {'name', 'resources'}:
        if key in document:
            config[key] = copy.deepcopy(document[key])
    issues = []
    for instance, block in document['resources'].items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', instance):
            issues.append(_issue('resources', message='리소스 이름을 확인하세요.'))
            continue
        field = 'resources.' + instance
        if (not isinstance(block, dict) or not {'adapter', 'inputs'} <= block.keys()
                or not set(block) <= {'adapter', 'inputs', 'policy'}
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
            resource = build_setup_resource(store, recipe, name, instance, block['inputs'], existing=existing)
            if 'policy' in block:
                resource['policy'] = copy.deepcopy(block['policy'])
            validate_resource(resource)
            validate_resource_policy(resource.get('policy', {}))
        except StoreError:
            issues.append(_issue(field, message='필수값·허용 정책·기존 연결 호환성을 확인하세요.'))
            continue
        if existing:
            config['resources'][config['resources'].index(existing)] = resource
        else:
            config['resources'].append(resource)
    for key in ('retention_days', 'long_term_memory_days'):
        if config[key] is not None and (type(config[key]) is not int or config[key] <= 0):
            issues.append(_issue(key, message='양의 정수 또는 null을 입력하세요.'))
    if not isinstance(config['core_context'], list) or any(not isinstance(v, str) for v in config['core_context']):
        issues.append(_issue('core_context'))
    try:
        _require(isinstance(config['embedding'], dict), '기억 검색 설정은 객체로 입력하세요.')
        config['embedding'] = validate_embedding_config(config['embedding'])
    except StoreError:
        issues.append(_issue('embedding', message='선택한 기억 검색 방식의 필수값과 전송 허용을 확인하세요.'))
    try:
        config['memory_policy'] = _profile_memory_policy(config['memory_policy'], config['resources'])
    except StoreError:
        issues.append(_issue('memory_policy'))
    return config, issues


def _inspect(store, name):
    path, metadata, meta, document, digest = _read_draft(store, name)
    profile = _current(store, name)
    if meta['base'] != _base(profile):
        raise StoreError('REVISION_CONFLICT', 'profile이 변경되었습니다. 변경을 확인하고 초안 기준을 갱신하세요.')
    for recipe, fingerprint in meta['schemas'].items():
        if recipe not in load_catalog() or fingerprint != _schema_digest(recipe):
            raise StoreError('REVISION_CONFLICT', '제품 입력 규격이 변경되었습니다. 초안을 확인하고 기준을 갱신하세요.')
    config, issues = _configuration(store, name, document, profile, meta)
    token = _digest([meta, digest]) if not issues else None
    preview = copy.deepcopy(document) if not issues else None
    if preview is not None:
        for instance, block in preview['resources'].items():
            resource = next(r for r in config['resources'] if r['instance_id'] == instance)
            block['inputs'] = _current_setup_values(resource, setup_schema(block['adapter']))
            block['policy'] = copy.deepcopy(resource['policy'])
        preview['embedding'] = copy.deepcopy(config['embedding'])
        preview['defaults'] = {'retention_days': config['retention_days'],
                               'long_term_memory_days': config['long_term_memory_days']}
        current_resources = {r['instance_id']: r for r in profile['resources']} if profile else {}
        preview['changes'] = {'created': profile is None,
                              'updated_resources': [r['instance_id'] for r in config['resources']
                                                    if r != current_resources.get(r['instance_id'])],
                              'settings': [key for key in sorted(PROFILE_FIELDS - {'name', 'resources'})
                                           if profile is None or config[key] != profile[key]],
                              'changed': profile is None or any(config[key] != profile[key] for key in PROFILE_FIELDS)}
    result = {'valid': not issues, 'complete': not issues, 'issues': issues,
              'preview': preview, 'confirmation_token': token}
    return result, config, profile, path, metadata, meta


def validate_draft(store, name):
    with store.transaction():
        try:
            return _inspect(store, name)[0]
        except StoreError as error:
            if error.code != 'INVALID_INPUT':
                raise
            return {'valid': False, 'complete': False,
                    'issues': [_issue('setup.yaml', message=error.message)],
                    'preview': None, 'confirmation_token': None}


def apply_draft(store, name, confirmation):
    with store.transaction():
        result, config, profile, path, metadata, meta = _inspect(store, name)
        token = result['confirmation_token']
        if (not token or not isinstance(confirmation, str) or not re.fullmatch('[0-9a-f]{64}', confirmation) or
                not hmac.compare_digest(confirmation, token)):
            raise StoreError('REVISION_CONFLICT', '현재 YAML의 검증 결과를 확인한 뒤 적용하세요.')
        if profile is None or any(config[key] != profile[key] for key in PROFILE_FIELDS):
            profile = store.put_profile(**config, create_only=profile is None)
        meta['base'] = _base(profile)
        _write(metadata, json.dumps(meta, ensure_ascii=False, allow_nan=False))
        return profile
