"""Lower MCP discovery and request interpretation, separated from task approval."""

import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp import types as mcp_types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .adapters import get_adapter
from .security import redact
from .storage import StoreError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SECRET_NAME = re.compile(r"(?i)(password|passwd|pwd|token|secret|api.?key|authorization|credential|database_uri|redis_url)")
_GUARDS = {"fixed", "allowed_values", "path_prefixes", "max_values", "sql_field", "tables", "columns", "max_rows", "region", "command_field"}
_SCOPE = {"equals", "one_of", "path_prefixes", "max_values", "tables", "columns", "max_rows"}


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError):
        raise StoreError("INVALID_INPUT", "A bounded JSON value is required.") from None


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _fail(message="The resource configuration is invalid."):
    raise StoreError("INVALID_INPUT", message)


def _known_error(error):
    if isinstance(error, StoreError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            found = _known_error(nested)
            if found:
                return found
    return None


def _schema(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        _fail("A top-level object schema is required.")
    def check(value):
        if isinstance(value, dict):
            if any(key in value for key in ("$dynamicRef", "$recursiveRef", "$id")):
                _fail("Only local self-contained schema references are supported.")
            if "$ref" in value and (not isinstance(value["$ref"], str) or not value["$ref"].startswith("#/")):
                _fail("External schema references are not supported.")
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        _fail("The resource schema is invalid.")


def _validate(value, schema, code="INVALID_INPUT"):
    try:
        Draft202012Validator(schema).validate(value)
    except Exception:
        raise StoreError(code, "The resource value does not satisfy its registered schema.") from None


def _guards(value, *, scope=False):
    if not isinstance(value, dict) or set(value) - (_SCOPE if scope else _GUARDS):
        _fail("Unknown scope constraints cannot be enforced.")
    for name in ("fixed", "equals", "allowed_values", "one_of", "path_prefixes", "max_values"):
        if name not in value:
            continue
        fields = value[name]
        if not isinstance(fields, dict) or any(not isinstance(key, str) or not key for key in fields):
            _fail()
        for field, item in fields.items():
            if name in ("allowed_values", "one_of", "path_prefixes") and (not isinstance(item, list) or not item):
                _fail()
            if name == "path_prefixes":
                from .adapters import clean_path
                for path in item:
                    clean_path(path)
            if name == "max_values" and (isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0):
                _fail()
    for name in ("tables", "columns"):
        if name in value and (not isinstance(value[name], list) or any(not isinstance(item, str) or not item for item in value[name])):
            _fail()
        if name in value and "*" in value[name] and value[name] != ["*"]:
            _fail("Whole DB access must use a standalone wildcard instead of a mixed list.")
    if "max_rows" in value and (type(value["max_rows"]) is not int or value["max_rows"] <= 0):
        _fail()
    _json(value)


def validate_resource(config):
    """Validate local administrator input without connecting or resolving secrets."""
    allowed = {"instance_id", "type", "adapter", "target", "identity_ref", "connection", "capabilities", "policy"}
    required = allowed - {"policy"}
    if not isinstance(config, dict) or set(config) - allowed or not required <= set(config):
        _fail()
    for name in ("instance_id", "type", "identity_ref"):
        if not isinstance(config[name], str) or not _ID.fullmatch(config[name]):
            _fail()
    adapter = get_adapter(config["adapter"])
    if config["type"] != adapter.resource_type or not isinstance(config["target"], dict) or not config["target"]:
        _fail()
    connection = config["connection"]
    if not isinstance(connection, dict):
        _fail()
    transport = connection.get("transport")
    if transport == "stdio":
        if set(connection) - {"transport", "command", "args", "env", "auth_env"}:
            _fail()
        command = connection.get("command")
        if not isinstance(command, str) or not Path(command).is_absolute():
            _fail("Register an absolute path to an already prepared executable.")
        if Path(command).stem.lower() in {"sh", "bash", "zsh", "cmd", "powershell", "pwsh"}:
            _fail("Shell launchers are not resource MCP executables.")
        if not isinstance(connection.get("args", []), list) or any(not isinstance(item, str) for item in connection.get("args", [])):
            _fail()
        for field in ("env", "auth_env"):
            mapping = connection.get(field, {})
            if not isinstance(mapping, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or not isinstance(v, str) for k, v in mapping.items()):
                _fail()
        if any(_SECRET_NAME.search(key) for key in connection.get("env", {})):
            _fail("Credential values belong in auth_env references, not env.")
    elif transport == "streamable_http":
        if set(connection) - {"transport", "url", "auth_headers"}:
            _fail()
        url = urlsplit(connection.get("url", ""))
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            _fail("Remote MCP requires an explicit HTTPS URL without embedded credentials.")
        headers = connection.get("auth_headers", {})
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", k) or not isinstance(v, str) for k, v in headers.items()):
            _fail()
        if any(key.lower() not in {"authorization", "x-api-key", "private-token"} for key in headers):
            _fail("Remote authentication headers cannot override HTTP routing or protocol headers.")
    else:
        _fail("Only stdio and HTTPS Streamable HTTP are supported.")
    adapter.validate_connection(config)
    capabilities = config["capabilities"]
    if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 128:
        _fail()
    ids = []
    for capability in capabilities:
        if (not isinstance(capability, dict) or set(capability) - {"capability_id", "tool_name", "description", "constraints"}
                or not {"capability_id", "tool_name", "constraints"} <= set(capability)
                or not isinstance(capability["capability_id"], str) or not _ID.fullmatch(capability["capability_id"])
                or capability["tool_name"] not in adapter.read_tools):
            _fail("Only supported, explicitly selected read tools may be registered.")
        ids.append(capability["capability_id"])
        _guards(capability["constraints"])
    if len(set(ids)) != len(ids):
        _fail()
    # The policy module validates the resource's separate model/export/retention policy.
    if "policy" in config and not isinstance(config["policy"], dict):
        _fail()
    serialized = _json(config)
    if len(serialized.encode()) > 262144 or redact(config) != config:
        _fail("Configuration contains credential-shaped text or exceeds its size limit.")
    return copy.deepcopy(config)


@dataclass
class NormalizedInvocation:
    invocation_ref: str
    instance_id: str
    capability_id: str
    contract_ref: str
    target_binding: dict
    identity_ref: str
    operation: str
    effects: dict
    access: dict
    prepared_request: dict
    controls: dict
    connection: dict
    tool_contract: dict
    fingerprint: str
    resource: dict
    binding_hash: str


class ResourceRegistry:
    def __init__(self, store, credential_resolver=None):
        self.store = store
        self.resolve = credential_resolver
        self.directory = store.data_dir / "resources"
        if self.directory.is_symlink():
            raise StoreError("STORE_UNAVAILABLE", "Resource registry directory is invalid.")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        key_path = self.directory / ".binding-key"
        if key_path.is_symlink():
            raise StoreError("STORE_UNAVAILABLE", "The credential binding key is invalid.")
        try:
            descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
        try:
            self.binding_key = key_path.read_bytes()
            if len(self.binding_key) != 32:
                raise ValueError
        except (OSError, ValueError):
            raise StoreError("STORE_UNAVAILABLE", "The credential binding key is unavailable.") from None

    def _binding(self, config):
        get_adapter(config["adapter"]).validate_connection(config)
        connection = config["connection"]
        refs = set(connection.get("auth_env", {}).values()) | set(connection.get("auth_headers", {}).values())
        credentials = {}
        for ref in refs:
            if not self.resolve:
                raise StoreError("AUTHENTICATION_REQUIRED", "A credential reference requires a configured resolver.")
            try:
                value = self.resolve(ref)
            except StoreError:
                raise
            except Exception:
                raise StoreError("AUTHENTICATION_REQUIRED", "The credential reference could not be resolved.") from None
            if not isinstance(value, str) or not value or any(char in value for char in "\r\n"):
                raise StoreError("AUTHENTICATION_REQUIRED", "The credential reference could not be resolved.")
            credentials[ref] = value
        provider_files = get_adapter(config["adapter"]).binding_files(config, credentials)
        return hmac.new(self.binding_key, _json([credentials, provider_files]).encode(), hashlib.sha256).hexdigest(), credentials

    def _resource(self, profile, instance_id):
        with self.store.transaction():
            self.store.assert_profile_available(profile['profile_id'])
        items = [resource for resource in profile.get("resources", []) if resource.get("instance_id") == instance_id]
        if len(items) != 1:
            raise StoreError("NOT_FOUND", "The resource is not registered in this profile.")
        return validate_resource(items[0])

    def _launch_connection(self, config):
        return get_adapter(config["adapter"]).launch_connection(config)

    def _path(self, profile, instance_id):
        return self.directory / (_hash([profile["profile_id"], instance_id]) + ".json")

    def _registration(self, profile, instance_id):
        config = self._resource(profile, instance_id)
        path = self._path(profile, instance_id)
        try:
            if path.is_symlink():
                raise ValueError
            registration = json.loads(path.read_bytes())
        except FileNotFoundError:
            raise StoreError("CONNECTION_NOT_READY", "Run explicit resource diagnosis before using this connection.") from None
        except (OSError, ValueError):
            raise StoreError("STORE_UNAVAILABLE", "The resource registration is unavailable.") from None
        if registration.get("profile_revision") != profile["revision"] or registration.get("config_hash") != _hash(config):
            raise StoreError("CONTEXT_CHANGED", "The profile or resource registration changed; run diagnosis again.")
        return config, registration

    def list(self, profile):
        items = []
        for resource in profile.get("resources", []):
            try:
                _, registration = self._registration(profile, resource["instance_id"])
                capabilities = [{"capability_id": item["capability_id"], "contract_ref": item["contract_ref"], "description": item["description"], "changes_state": False} for item in registration["descriptors"]]
            except StoreError:
                capabilities = []
            items.append({"instance_id": resource["instance_id"], "type": resource["type"], "label": resource["instance_id"],
                          "capability_summaries": capabilities})
        return items

    def get(self, profile, instance_id, capability_id):
        _, registration = self._registration(profile, instance_id)
        for descriptor in registration["descriptors"]:
            if descriptor["capability_id"] == capability_id:
                return copy.deepcopy(descriptor)
        raise StoreError("NOT_FOUND", "The capability is not registered in this profile.")

    async def _session(self, stack, connection, timeout, credentials):
        secrets = []
        def resolve(ref):
            value = credentials.get(ref)
            if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
                raise StoreError("AUTHENTICATION_REQUIRED", "The credential reference could not be resolved.")
            secrets.append(value)
            if "://" in value:
                uri = urlsplit(value)
                if uri.password:
                    secrets.append(unquote(uri.password))
            return value
        if connection["transport"] == "stdio":
            env = dict(connection.get("env", {}))
            env.update({key: resolve(ref) for key, ref in connection.get("auth_env", {}).items()})
            # SDK itself inherits a small platform allowlist, never arbitrary parent credentials.
            sink = stack.enter_context(open(os.devnull, "w"))
            working = self.directory / "processes" / _hash(connection)
            working.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.directory.is_symlink() or working.parent.is_symlink() or working.is_symlink() or any(working.iterdir()):
                raise StoreError("CONNECTION_NOT_READY", "The isolated lower MCP working directory must be empty.")
            read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(
                command=connection["command"], args=connection.get("args", []), env=env, cwd=working), errlog=sink))
        else:
            headers = {key: resolve(ref) for key, ref in connection.get("auth_headers", {}).items()}
            client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, trust_env=False))
            read, write, _ = await stack.enter_async_context(streamable_http_client(connection["url"], http_client=client))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session, secrets

    async def _tools(self, session):
        result, cursor, pages = [], None, 0
        while True:
            page = await session.list_tools(cursor=cursor)
            result.extend(tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in page.tools)
            pages += 1
            if len(result) > 512 or pages > 8:
                raise StoreError("UNSUPPORTED_CONSTRAINT", "The lower tool registry exceeds the bounded discovery limit.")
            cursor = page.nextCursor
            if not cursor:
                break
        if len({item["name"] for item in result}) != len(result):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "The lower tool registry has ambiguous names.")
        return result

    def diagnose(self, profile, instance_id, timeout=30):
        with self.store.profile_operation(profile['profile_id']):
            return self._diagnose(profile, instance_id, timeout)

    def _diagnose(self, profile, instance_id, timeout):
        config = self._resource(profile, instance_id)
        binding_hash, credentials = self._binding(config)
        async def run():
            async with asyncio.timeout(timeout):
                async with AsyncExitStack() as stack:
                    session, _ = await self._session(stack, self._launch_connection(config), timeout, credentials)
                    return await self._tools(session)
        try:
            tools = asyncio.run(run())
        except StoreError:
            raise
        except Exception as error:
            known = _known_error(error)
            if known:
                raise known from None
            raise StoreError("CONNECTION_NOT_READY", "Resource diagnosis failed; check the executable, authentication, and connection.") from None
        descriptors, contracts = [], {}
        for capability in config["capabilities"]:
            tool = next((item for item in tools if item["name"] == capability["tool_name"]), None)
            if tool is None:
                raise StoreError("NOT_FOUND", "A selected tool is missing from the lower MCP registry.")
            _schema(tool["inputSchema"])
            if "outputSchema" in tool:
                _schema(tool["outputSchema"])
            contracts[capability["capability_id"]] = tool
            contract_ref = "contract_" + _hash(["daeri-read-normalization-v1", config, capability, tool, binding_hash])
            descriptors.append(self._descriptor(config, capability, tool, contract_ref))
        registration = {"profile_revision": profile["revision"], "config_hash": _hash(config), "binding_hash": binding_hash, "descriptors": descriptors, "contracts": contracts}
        path = self._path(profile, instance_id)
        temporary = path.with_name(uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                temporary.chmod(0o600)
                stream.write(_json(registration))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except OSError:
            raise StoreError("STORE_UNAVAILABLE", "The resource diagnosis could not be saved.") from None
        return {"instance_id": instance_id, "ready": True, "capability_count": len(descriptors), "contract_refs": [item["contract_ref"] for item in descriptors]}

    def _descriptor(self, config, capability, tool, contract_ref):
        return {"instance_id": config["instance_id"], "capability_id": capability["capability_id"], "contract_ref": contract_ref,
                "description": capability.get("description", tool.get("description", capability["tool_name"])),
                "input_schema": tool["inputSchema"], "output_schema": get_adapter(config["adapter"]).output_schema(tool),
                "scope_schema": {"type": "object", "properties": {field: {"type": "integer" if field == "max_rows" else "array" if field in ("tables", "columns") else "object"} for field in _SCOPE}, "additionalProperties": False,
                                 "x-daeri-access-constraints": copy.deepcopy(capability["constraints"])},
                "supported_purposes": ["collect", "verify"], "supported_metrics": [],
                "budget_support": {"external_requests_enforceable": False},
                "effects": {"changes_state": False, "side_effects": ["access logs, possible provider charges and service load"], "interruptible": False},
                "retry": {"auto_retry_supported": False, "max_attempts": 1, "read_repeatability": "live", "retryable_codes": []},
                "idempotency": {"supported": False, "key_mode": "none", "key_pointer": None, "scope": "No lower MCP duplicate suppression guarantee.", "validity": "Not supported."},
                "verification": {"supported": False, "capability_ids": [], "external_reference_pointers": []},
                "continuation": {"supported": False, "cursor_input_pointer": None, "next_cursor_output_pointer": None, "has_more_output_pointer": None, "consistency": "live", "validity": None},
                "metadata_discovery_allowed": True}

    def normalize(self, profile, instance_id, capability_id, parameters, scope):
        config, registration = self._registration(profile, instance_id)
        binding_hash, _ = self._binding(config)
        if binding_hash != registration.get("binding_hash"):
            raise StoreError("CONTEXT_CHANGED", "Credentials or provider authentication files changed after diagnosis.")
        descriptor = self.get(profile, instance_id, capability_id)
        capability = next(item for item in config["capabilities"] if item["capability_id"] == capability_id)
        if not isinstance(parameters, dict) or len(_json(parameters).encode()) > 65536:
            _fail()
        _guards(scope, scope=True)
        arguments = copy.deepcopy(parameters)
        for field, value in capability["constraints"].get("fixed", {}).items():
            if field in arguments and (type(arguments[field]) is not type(value) or arguments[field] != value):
                raise StoreError("SCOPE_VIOLATION", "The configured target cannot be overridden.")
            arguments[field] = copy.deepcopy(value)
        properties = descriptor["input_schema"].get("properties", {})
        for field, schema in properties.items():
            if field not in arguments and isinstance(schema, dict) and "default" in schema and schema["default"] is not None:
                arguments[field] = copy.deepcopy(schema["default"])
        adapter = get_adapter(config["adapter"])
        adapter.prepare_parameters(arguments, descriptor["input_schema"])
        _validate(arguments, descriptor["input_schema"])
        access = adapter.normalize(arguments, capability["constraints"], scope)
        _validate(arguments, descriptor["input_schema"])
        prepared = {"tool_name": capability["tool_name"], "arguments": arguments}
        return NormalizedInvocation("invocation_" + uuid4().hex, instance_id, capability_id, descriptor["contract_ref"],
            copy.deepcopy(config["target"]), config["identity_ref"], capability["tool_name"], descriptor["effects"], access,
            prepared, {"external_requests_enforceable": False, "auto_retry": False}, copy.deepcopy(config["connection"]),
            registration["contracts"][capability_id], _hash(prepared), config, binding_hash)

    def execute(self, invocation, timeout=30):
        if not isinstance(invocation, NormalizedInvocation) or _hash(invocation.prepared_request) != invocation.fingerprint:
            raise StoreError("CONTEXT_CHANGED", "The normalized request changed after validation.")
        started = monotonic()
        adapter = get_adapter(invocation.resource["adapter"])
        binding_hash, credentials = self._binding(invocation.resource)
        if binding_hash != invocation.binding_hash or invocation.connection != invocation.resource["connection"]:
            raise StoreError("CONTEXT_CHANGED", "The authentication context changed after request validation.")
        async def run():
            async with asyncio.timeout(timeout):
                async with AsyncExitStack() as stack:
                    session, secrets = await self._session(stack, self._launch_connection(invocation.resource), timeout, credentials)
                    tools = await self._tools(session)
                    current = next((item for item in tools if item["name"] == invocation.operation), None)
                    if current != invocation.tool_contract:
                        raise StoreError("CONTEXT_CHANGED", "The lower tool contract changed after approval.")
                    # Validate against the exact pinned schema here; avoid an SDK refresh
                    # or a schema exception that obscures the confirmed tool response.
                    result = await session.send_request(mcp_types.ClientRequest(mcp_types.CallToolRequest(
                        params=mcp_types.CallToolRequestParams(name=invocation.operation,
                            arguments=invocation.prepared_request["arguments"]))), mcp_types.CallToolResult)
                    if result.isError:
                        raise StoreError("EXTERNAL_EXECUTION_FAILED", "The lower MCP reported a failed operation; its untrusted error body is withheld.")
                    value = result.structuredContent
                    schema = invocation.tool_contract.get("outputSchema")
                    if schema is not None:
                        _validate(value, schema, "RESULT_VALIDATION_FAILED")
                    value_types = {}
                    value, schema = adapter.decode_result(result, invocation.tool_contract, value_types)
                    adapter.provider_error(value)
                    if len(_json(value).encode()) > 2097152:
                        raise StoreError("RESULT_TRUNCATED", "The lower result exceeds the bounded result limit; no incomplete body is exposed.")
                    encoded = _json(redact(value))
                    for secret in secrets:
                        encoded = encoded.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
                    sanitized = json.loads(encoded)
                    if schema is not None:
                        _validate(sanitized, schema, "RESULT_VALIDATION_FAILED")
                    return sanitized, value_types
        try:
            result, value_types = asyncio.run(run())
        except StoreError:
            raise
        except Exception as error:
            known = _known_error(error)
            if known:
                raise known from None
            raise StoreError("EXTERNAL_OUTCOME_UNKNOWN", "The lower MCP disconnected or timed out; execution is not automatically retried.") from None
        return {"result": result, "value_types": value_types, "units": {}, "warnings": adapter.warnings(result), "source_complete": adapter.source_complete(result, invocation),
                "external_requests": None, "runtime_ms": max(0, int((monotonic() - started) * 1000))}
