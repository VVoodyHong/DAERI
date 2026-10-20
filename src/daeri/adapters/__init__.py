"""Trusted product read adapters; identifier aliases never rewrite stored configuration."""

from .aws import AwsReadAdapter
from .base import ReadAdapter, clean_path
from .confluence import ConfluenceReadAdapter
from .gitlab import GitlabReadAdapter
from .postgres import PostgresReadAdapter
from .redis import RedisReadAdapter
from ..storage import StoreError


def _build_registry(adapters):
    registry, namespace = {}, set()
    for adapter in adapters:
        if not isinstance(adapter, ReadAdapter):
            raise StoreError("INVALID_INPUT", "A trusted read adapter is required.")
        names = (adapter.adapter_id, *adapter.aliases)
        if (any(not isinstance(name, str) or not name for name in names)
                or len(set(names)) != len(names) or namespace.intersection(names)):
            raise StoreError("INVALID_INPUT", "Resource adapter names and aliases must be unique.")
        namespace.update(names)
        registry[adapter.adapter_id] = adapter
    return registry


BUILTIN_ADAPTERS = _build_registry((
    PostgresReadAdapter(), RedisReadAdapter(), AwsReadAdapter(), GitlabReadAdapter(), ConfluenceReadAdapter(),
))


def get_adapter(identifier):
    if isinstance(identifier, str) and identifier:
        for adapter in _build_registry(BUILTIN_ADAPTERS.values()).values():
            if identifier == adapter.adapter_id or identifier in adapter.aliases:
                return adapter
    raise StoreError("INVALID_INPUT", "The resource adapter is not supported.")


def normalize(adapter, parameters, constraints, scope):
    return get_adapter(adapter).normalize(parameters, constraints, scope)
