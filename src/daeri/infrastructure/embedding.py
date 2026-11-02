"""Explicit embedding configuration and a bounded, credential-referenced API adapter."""

from contextlib import nullcontext
import hashlib
import json
import re
from threading import Lock
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

from daeri.core.security import redact
from daeri.core.errors import StoreError
from daeri.core.serialization import validated_float32_vector


_REFERENCE = re.compile(r"(?:credential:[A-Za-z0-9._-]{1,128}|env:[A-Za-z_][A-Za-z0-9_]*)")
_SECRET_QUERY = re.compile(r"password|passwd|token|secret|key|authorization|credential|signature|^sig$", re.I)
_TOKEN = re.compile(r"(?:\bsk-[A-Za-z0-9_-]{12,}|\bglpat-[A-Za-z0-9_-]{12,})")
_OPENAI_FIELDS = {"provider", "adapter", "endpoint", "model", "credential_ref", "dimensions", "timeout_seconds"}


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
    _valid(isinstance(value, dict) and value.get('provider') == 'api'
           and value.get('adapter') == 'openai_compatible')
    _valid(set(value) <= _OPENAI_FIELDS and {"endpoint", "model", "credential_ref"} <= value.keys())
    connection = validate_api_connection(value['endpoint'], value['model'], value['credential_ref'],
                                         value.get('timeout_seconds', 15))
    if "dimensions" in value:
        _valid(type(value["dimensions"]) is int and 1 <= value["dimensions"] <= 8192)
    return {**value, **connection}


def validate_api_endpoint(endpoint):
    """Require an explicit HTTPS request URL without inline credentials."""
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
    return endpoint


def validate_api_connection(endpoint, model, credential_ref, timeout_seconds=15):
    validate_api_endpoint(endpoint)
    _valid(_safe_text(model, 256))
    _valid(isinstance(credential_ref, str) and bool(_REFERENCE.fullmatch(credential_ref)))
    _valid(type(timeout_seconds) is int and 1 <= timeout_seconds <= 60)
    return {'endpoint': endpoint, 'model': model, 'credential_ref': credential_ref, 'timeout_seconds': timeout_seconds}


class OpenAICompatibleEmbedder:
    """Call the configured embeddings endpoint with explicitly supplied credential resolution."""

    def __init__(self, config, *, credentials, client=None):
        self.config = validate_embedding_config(config)
        self.credentials = credentials
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
            vector = validated_float32_vector(item.get("embedding"))
            actual_model = result.get("model")
            if actual_model is not None and not _safe_text(actual_model, 256):
                raise ValueError
            with self._identity_lock:
                if ((self.dimensions is not None and self.dimensions != len(vector))
                        or (self._initialized and actual_model != self._response_model)):
                    raise ValueError
                self.dimensions = len(vector)
                self._response_model, self._initialized = actual_model, True
            return vector
        except Exception:
            # Provider bodies and transport exceptions can contain credentials or source text.
            raise StoreError("MEMORY_INDEX_INCOMPLETE", "회사 임베딩 API의 인증·연결·응답을 확인할 수 없어 벡터 처리를 완료하지 못했습니다.") from None
