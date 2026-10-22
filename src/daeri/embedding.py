"""Explicit embedding configuration and a bounded, credential-referenced API adapter."""

from contextlib import nullcontext
import hashlib
import json
import math
import re
import struct
from threading import Lock
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

from .credentials import Credentials
from .security import redact
from .storage import StoreError


_REFERENCE = re.compile(r"(?:credential:[A-Za-z0-9._-]{1,128}|env:[A-Za-z_][A-Za-z0-9_]*)")
_SECRET_QUERY = re.compile(r"password|passwd|token|secret|key|authorization|credential|signature|^sig$", re.I)
_TOKEN = re.compile(r"(?:\bsk-[A-Za-z0-9_-]{12,}|\bglpat-[A-Za-z0-9_-]{12,})")
_OPENAI_FIELDS = {"provider", "adapter", "endpoint", "model", "credential_ref", "allow_remote", "dimensions", "timeout_seconds"}


def _valid(condition):
    if not condition:
        raise StoreError("INVALID_INPUT", "임베딩 설정이 올바르지 않습니다. 인증 정보는 참조로 등록하세요.")


def _safe_text(value, limit):
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return (value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and redact(value) == value and not _TOKEN.search(value))


def validate_embedding_config(value):
    """Return a nonsecret canonical setting without connecting or resolving credentials."""
    if value is None:
        return {"provider": "local"}
    _valid(isinstance(value, dict) and isinstance(value.get("provider"), str))
    if value['provider'] == 'openai_compatible':
        _valid(value.get('adapter', 'openai_compatible') == 'openai_compatible')
        value = {**value, 'provider': 'api', 'adapter': 'openai_compatible'}
    kind = value["provider"]
    _valid(kind in {"local", "api", "keyword_only"})
    if kind != "api":
        _valid(set(value) == {"provider"})
        return {"provider": kind}
    adapter = value.get('adapter')
    _valid(isinstance(adapter, str) and adapter in API_ADAPTERS)
    _valid(type(value.get('allow_remote', False)) is bool)
    selected = API_ADAPTERS[adapter].validate_config(value)
    return {**selected, 'provider': 'api', 'adapter': adapter,
            'allow_remote': value.get('allow_remote', False)}


def _validate_openai_config(value):
    _valid(set(value) <= _OPENAI_FIELDS and {"endpoint", "model", "credential_ref"} <= value.keys())
    endpoint = value["endpoint"]
    _valid(_safe_text(endpoint, 2048) and _safe_text(unquote(endpoint), 2048))
    try:
        url = urlsplit(endpoint)
        _valid(url.scheme == "https" and bool(url.hostname) and url.username is None
               and url.password is None and "#" not in endpoint and bool(url.path)
               and url.path != "/" and not any(char.isspace() for char in endpoint))
        _valid(url.port is None or 1 <= url.port <= 65535)
        _valid(not any(_SECRET_QUERY.search(key) for key, _ in parse_qsl(url.query, keep_blank_values=True)))
        httpx.URL(endpoint)
    except (ValueError, httpx.InvalidURL):
        raise StoreError("INVALID_INPUT", "HTTPS 임베딩 요청 주소가 올바르지 않습니다.") from None
    _valid(_safe_text(value["model"], 256))
    _valid(isinstance(value["credential_ref"], str) and bool(_REFERENCE.fullmatch(value["credential_ref"])))
    _valid(type(value.get("allow_remote", False)) is bool)
    _valid(type(value.get("timeout_seconds", 15)) is int and 1 <= value.get("timeout_seconds", 15) <= 60)
    if "dimensions" in value:
        _valid(type(value["dimensions"]) is int and 1 <= value["dimensions"] <= 8192)
    return {**value, "allow_remote": value.get("allow_remote", False), "timeout_seconds": value.get("timeout_seconds", 15)}


class OpenAICompatibleEmbedder:
    """Send only caller-approved text to an explicitly configured embeddings endpoint."""

    remote = True
    label = 'OpenAI 호환 API'
    description = 'OpenAI embeddings 요청·응답 형식과 Bearer 인증을 사용합니다.'
    setup_credentials = {'credential_ref': {'field': 'api_key', 'description': '회사 임베딩 API의 인증 키입니다.'}}
    field_comments = {
        'adapter': '회사 API의 요청·응답·인증 규격입니다.\n'
                   'openai_compatible은 OpenAI 호환 형식과 Bearer 인증을 사용합니다.',
        'endpoint': '회사가 제공한 정확한 HTTPS 임베딩 요청 주소입니다.\n'
                    '가상 예: https://embedding.example.test/v1/embeddings',
        'model': '회사에서 지정한 모델 또는 배포 이름입니다. 가상 예: company-embedding',
        'credential_ref': '인증 참조입니다. credential:등록이름 또는 env:환경변수명을 입력합니다.\n'
                          '인증값은 credentials.api_key에 입력합니다. 적용하면 이 참조의 인증 저장소에 보관합니다.',
        'dimensions': 'API가 지원하는 벡터 차원입니다(정수 1~8192). 생략하면 응답의 차원을 사용합니다.',
        'timeout_seconds': '네트워크 단계별 대기 제한(초)입니다. 정수 1~60, 기본값은 15입니다.',
    }
    validate_config = staticmethod(_validate_openai_config)

    @staticmethod
    def draft_config(name):
        return {'provider': 'api', 'adapter': 'openai_compatible', 'endpoint': None, 'model': None,
                'credential_ref': 'credential:' + name + '.embedding', 'allow_remote': False}

    def __init__(self, config, credentials=None, client=None):
        self.config = validate_embedding_config(config)
        _valid(self.config['provider'] == 'api' and self.config['adapter'] == 'openai_compatible')
        self.credentials = credentials if credentials is not None else Credentials()
        self.client = client
        self.dimensions = self.config.get("dimensions")
        self._initialized = False
        self._response_model = None
        self._identity_lock = Lock()

    @property
    def model_id(self):
        identity = {key: self.config.get(key) for key in ("endpoint", "model", "dimensions")}
        with self._identity_lock:
            identity.update(preprocessing="plain-v1", response_model=self._response_model)
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return "openai-compatible:" + hashlib.sha256(encoded).hexdigest()

    def embed_query(self, text):
        return self._embed(text)

    def embed_document(self, text):
        return self._embed(text)

    def _embed(self, text):
        if not self.config["allow_remote"]:
            raise StoreError("ACCESS_DENIED", "회사 임베딩 API로 기억과 검색 질의를 전송하는 허용이 필요합니다.")
        try:
            if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 65536:
                raise ValueError
            token = self.credentials.get(self.config["credential_ref"])
            if not isinstance(token, str) or not token.strip() or any(ord(char) < 32 for char in token):
                raise ValueError
            body = {"model": self.config["model"], "input": text, "encoding_format": "float"}
            if "dimensions" in self.config:
                body["dimensions"] = self.config["dimensions"]
            context = nullcontext(self.client) if self.client is not None else httpx.Client()
            with context as client:
                with client.stream("POST", self.config["endpoint"], json=body,
                                   headers={"Authorization": "Bearer " + token},
                                   timeout=self.config["timeout_seconds"], follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise ValueError
                    data = bytearray()
                    for block in response.iter_bytes(chunk_size=65536):
                        if len(data) + len(block) > 1024 * 1024:
                            raise ValueError
                        data.extend(block)
            result = json.loads(data)
            if not isinstance(result, dict) or not isinstance(result.get("data"), list) or len(result["data"]) != 1:
                raise ValueError
            item = result["data"][0]
            if not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != 0:
                raise ValueError
            vector = item.get("embedding")
            if (not isinstance(vector, list) or not 1 <= len(vector) <= 8192
                    or any(type(number) not in (int, float) or not math.isfinite(number) for number in vector)
                    or not any(vector)):
                raise ValueError
            # The local vector index stores float32; finite float64 can overflow or become all zero there.
            stored = struct.unpack(f"<{len(vector)}f", struct.pack(f"<{len(vector)}f", *vector))
            if not all(math.isfinite(number) for number in stored) or not any(stored):
                raise ValueError
            actual_model = result.get("model")
            if actual_model is not None and not _safe_text(actual_model, 256):
                raise ValueError
            with self._identity_lock:
                if ((self.dimensions is not None and self.dimensions != len(vector))
                        or (self._initialized and actual_model != self._response_model)):
                    raise ValueError
                self.dimensions = len(vector)
                self._response_model, self._initialized = actual_model, True
            return [float(number) for number in vector]
        except Exception:
            # Provider bodies and transport exceptions can contain credentials or source text.
            raise StoreError("MEMORY_INDEX_INCOMPLETE", "회사 임베딩 API의 인증·연결·응답을 확인할 수 없어 벡터 처리를 완료하지 못했습니다.") from None


API_ADAPTERS = {'openai_compatible': OpenAICompatibleEmbedder}


def embedding_credential_fields(config):
    """Describe credential inputs of the selected API adapter, including partial drafts."""
    if not isinstance(config, dict) or config.get('provider') not in ('api', 'openai_compatible'):
        return {}
    name = config.get('adapter', 'openai_compatible' if config['provider'] == 'openai_compatible' else None)
    if not isinstance(name, str) or name not in API_ADAPTERS:
        return {}
    fields = {}
    for config_key, metadata in getattr(API_ADAPTERS[name], 'setup_credentials', {}).items():
        field = metadata['field']
        _valid(field not in fields)
        fields[field] = {'reference': config.get(config_key), 'description': metadata['description'],
                         'config_key': config_key}
    return fields


def embedding_field_comments(config):
    """Describe common fields and the selected adapter's public YAML inputs."""
    if not isinstance(config, dict):
        return {}
    comments = {'provider': '임베딩 방식입니다. local: 로컬 모델 / api: 회사 API / keyword_only: 키워드 검색.'}
    if config.get('provider') in ('api', 'openai_compatible'):
        adapter = config.get('adapter', 'openai_compatible' if config['provider'] == 'openai_compatible' else None)
        if isinstance(adapter, str) and adapter in API_ADAPTERS:
            comments.update(getattr(API_ADAPTERS[adapter], 'field_comments', {}))
        comments.setdefault('adapter', '회사 API의 연결 규격입니다. 지원 목록에서 확인한 연결 방식 ID를 입력합니다.')
        comments['allow_remote'] = ('true: 허용된 기억·검색 질의를 지정한 회사 API로 전송합니다.\n'
                                    'false: API 전송을 보류합니다(기본값).')
    return comments


def api_adapter_catalog():
    return [{'adapter_id': name, 'label': adapter.label, 'description': adapter.description}
            for name, adapter in API_ADAPTERS.items()]


def api_embedding_draft(name, adapter):
    if adapter is None:
        return {'provider': 'api', 'adapter': None}
    _valid(isinstance(adapter, str) and adapter in API_ADAPTERS)
    return API_ADAPTERS[adapter].draft_config(name)


def create_api_embedder(config, *, credentials=None, client=None):
    selected = validate_embedding_config(config)
    _valid(selected['provider'] == 'api')
    return API_ADAPTERS[selected['adapter']](selected, credentials=credentials, client=client)
