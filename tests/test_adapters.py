"""Adapter selection, compatibility, and real lower-MCP extension contracts."""

import copy
import json
import sys
from pathlib import Path

import pytest

from daeri.infrastructure.resources import ResourceRegistry, validate_resource
from daeri.infrastructure.storage import Store
from daeri.core.errors import StoreError


@pytest.fixture
def registry(tmp_path):
    store = Store(tmp_path / "private")
    yield ResourceRegistry(store)
    store.close()


def resource_config(adapter_id):
    return {
        "instance_id": "source-one", "type": "Source", "adapter": adapter_id,
        "target": {"server": "https://git.example.test", "project": "team/service"},
        "identity_ref": "identity:reader",
        "connection": {
            "transport": "stdio", "command": sys.executable,
            "args": [str(Path(__file__).parent / "fixtures" / "resource_mcp.py")],
            "env": {"GITLAB_URL": "https://git.example.test"},
        },
        "capabilities": [{
            "capability_id": "read-file", "tool_name": "get_repository_file",
            "constraints": {
                "fixed": {"project_id": "team/service"},
                "allowed_values": {"ref": ["main"]},
                "path_prefixes": {"file_path": ["src"]},
            },
        }],
    }


def exercise_stdio_read(registry, resource):
    original = copy.deepcopy(resource)
    assert validate_resource(resource) == original
    profile = {"profile_id": "profile_one", "revision": 1, "resources": [resource]}
    original_profile = copy.deepcopy(profile)

    diagnosis = registry.diagnose(profile, "source-one")
    assert diagnosis["ready"] is True
    assert diagnosis["capability_count"] == 1
    assert profile == original_profile

    invocation = registry.normalize(
        profile, "source-one", "read-file",
        {"ref": "main", "file_path": "src/main.py"},
        {"equals": {"project_id": "team/service", "ref": "main"},
         "path_prefixes": {"file_path": ["src"]}},
    )
    expected_arguments = {
        "project_id": "team/service", "ref": "main", "file_path": "src/main.py", "limit": 20,
    }
    assert invocation.prepared_request == {
        "tool_name": "get_repository_file", "arguments": expected_arguments,
    }
    assert invocation.access == {"fields": ["file_path", "limit", "project_id", "ref"]}
    assert profile == original_profile

    result = registry.execute(invocation)
    body = result["result"]["body"]
    assert json.loads(body.split(" ", 1)[1]) == expected_arguments
    assert "remove-this" not in body
    assert profile == original_profile
    assert resource == original


@pytest.mark.parametrize("adapter_id,resource_type,read_tool", [
    ("postgres_read", "DB", "execute_sql"),
    ("redis_read", "Cache", "get"),
    ("aws_read", "Infra", "call_aws"),
    ("gitlab_read", "Source", "get_repository_file"),
    ("confluence_read", "Document", "confluence_get_page"),
    ("jira_read", "Issue", "jira_get_issue"),
])
def test_builtin_selection_exposes_the_read_adapter_contract(adapter_id, resource_type, read_tool):
    import daeri.infrastructure.adapters as adapters

    get_adapter = getattr(adapters, "get_adapter")
    from daeri.infrastructure.adapters.base import ReadAdapter

    adapter = get_adapter(adapter_id)
    assert isinstance(adapter, ReadAdapter)
    assert adapter.adapter_id == adapter_id
    assert adapter.resource_type == resource_type
    assert all(isinstance(alias, str) for alias in adapter.aliases)
    assert read_tool in adapter.read_tools
    assert isinstance(adapter.target_template, dict)
    assert adapter.target_template


def test_builtin_registry_maps_canonical_ids_to_the_selected_instances():
    import daeri.infrastructure.adapters as adapters

    get_adapter = getattr(adapters, "get_adapter")
    registered = getattr(adapters, "BUILTIN_ADAPTERS")
    assert isinstance(registered, dict)
    assert set(registered) == {
        "postgres_read", "redis_read", "aws_read", "gitlab_read", "confluence_read", "jira_read",
    }
    for adapter_id, adapter in registered.items():
        assert get_adapter(adapter_id) is adapter


@pytest.mark.parametrize("legacy,canonical", [
    ("sql_read", "postgres_read"),
    ("source_read", "gitlab_read"),
    ("document_read", "confluence_read"),
])
def test_legacy_names_select_the_same_adapter_instance(legacy, canonical):
    import daeri.infrastructure.adapters as adapters

    get_adapter = getattr(adapters, "get_adapter")
    adapter = get_adapter(canonical)
    assert get_adapter(legacy) is adapter
    assert legacy in adapter.aliases


@pytest.mark.parametrize("adapter_id", [
    "unknown_read", "", None, 1, True, [], {}, b"source_read",
])
def test_unknown_and_nonstring_adapter_ids_are_invalid_input(adapter_id):
    import daeri.infrastructure.adapters as adapters

    get_adapter = getattr(adapters, "get_adapter")
    with pytest.raises(StoreError) as caught:
        get_adapter(adapter_id)
    assert caught.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("adapter_id", ["gitlab_read", "source_read"])
def test_resource_category_must_match_the_selected_adapter(adapter_id):
    resource = resource_config(adapter_id)
    resource["type"] = "Document"
    original = copy.deepcopy(resource)
    with pytest.raises(StoreError) as caught:
        validate_resource(resource)
    assert caught.value.code == "INVALID_INPUT"
    assert resource == original


@pytest.mark.parametrize("adapter_id", ["gitlab_read", "source_read"])
def test_canonical_and_legacy_gitlab_configs_execute_without_rewriting(registry, adapter_id):
    exercise_stdio_read(registry, resource_config(adapter_id))


def test_registered_read_adapter_subclass_uses_the_common_stdio_pipeline(registry, monkeypatch):
    import daeri.infrastructure.adapters as adapters

    get_adapter = getattr(adapters, "get_adapter")
    from daeri.infrastructure.adapters.base import ReadAdapter

    class FixtureReadAdapter(ReadAdapter):
        adapter_id = "fixture_read"
        resource_type = "Source"
        read_tools = {"get_repository_file"}
        target_template = {"server": "https://fixture.invalid"}

    adapter = FixtureReadAdapter()
    monkeypatch.setitem(adapters.BUILTIN_ADAPTERS, "fixture_read", adapter)
    assert get_adapter("fixture_read") is adapter

    resource = resource_config("fixture_read")
    resource["target"] = {"server": "https://fixture.invalid"}
    resource["connection"]["env"] = {}
    exercise_stdio_read(registry, resource)

    mismatched = copy.deepcopy(resource)
    mismatched["type"] = "Document"
    with pytest.raises(StoreError) as caught:
        validate_resource(mismatched)
    assert caught.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("identifier", ["gitlab_read", "source_read"])
def test_ambiguous_trusted_adapter_names_are_rejected(identifier, monkeypatch):
    import daeri.infrastructure.adapters as adapters
    from daeri.infrastructure.adapters.base import ReadAdapter

    class ConflictingAdapter(ReadAdapter):
        adapter_id = "extension_read"
        aliases = (identifier,)

    monkeypatch.setitem(adapters.BUILTIN_ADAPTERS, "extension_read", ConflictingAdapter())
    with pytest.raises(StoreError, match="INVALID_INPUT"):
        adapters.get_adapter("extension_read")


def test_registry_builder_rejects_duplicate_canonical_ids_before_dictionary_creation():
    import daeri.infrastructure.adapters as adapters
    from daeri.infrastructure.adapters.base import ReadAdapter

    class DuplicateAdapter(ReadAdapter):
        adapter_id = "gitlab_read"

    with pytest.raises(StoreError, match="INVALID_INPUT"):
        adapters._build_registry((adapters.get_adapter("gitlab_read"), DuplicateAdapter()))


@pytest.mark.parametrize("adapter_id", ["postgres_read", "redis_read", "aws_read", "gitlab_read", "confluence_read"])
@pytest.mark.parametrize("field", [
    "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
    "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
])
def test_control_environment_names_keep_the_existing_case_sensitive_boundary(adapter_id, field):
    from daeri.infrastructure.adapters import get_adapter

    resource = resource_config(adapter_id)
    resource["connection"]["env"][field.lower()] = "false"
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        get_adapter(adapter_id).validate_connection(resource)
