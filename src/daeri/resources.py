"""Lower MCP discovery and request interpretation, separated from task approval."""

import asyncio
import ast
import copy
import configparser
import hashlib
import hmac
import json
import os
import re
from decimal import Decimal, InvalidOperation
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp import types as mcp_types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .adapters import READ_TOOLS, normalize
from .security import redact
from .storage import StoreError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SECRET_NAME = re.compile(r"(?i)(password|passwd|pwd|token|secret|api.?key|authorization|credential|database_uri|redis_url)")
_GUARDS = {"fixed", "allowed_values", "path_prefixes", "max_values", "sql_field", "tables", "columns", "max_rows", "region", "command_field"}
_SCOPE = {"equals", "one_of", "path_prefixes", "max_values", "tables", "columns", "max_rows"}
_LEGACY_SCHEMA = {"type": "object", "properties": {"data": {}}, "required": ["data"], "additionalProperties": False}
_NORMALIZED_ADAPTERS = {"sql_read", "redis_read", "aws_read"}
_AWS_ENV = {"AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_API_MCP_PROFILE_NAME", "AWS_API_MCP_TELEMETRY"}


def _connection_controls(config):
    connection = config["connection"]
    if connection["transport"] != "stdio":
        return
    env = connection.get("env", {})
    forbidden = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS", "BASH_ENV", "ENV", "SHELLOPTS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    if any(key.upper() in forbidden or key.upper().startswith(("DYLD_", "LD_")) for key in (*env, *connection.get("auth_env", {}))):
        raise StoreError("UNSUPPORTED_CONSTRAINT", "Program injection and implicit proxy environment overrides are unsupported.")
    adapter = config["adapter"]
    prefixes = {"sql_read": ("PG",), "redis_read": ("REDIS_",), "source_read": ("GITLAB_",),
                "document_read": ("CONFLUENCE_", "OAUTH_"), "aws_read": ("AWS_",)}
    modes = {"READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
             "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR"}
    if any(key != key.upper() and (key.upper().startswith(prefixes[adapter]) or key.upper() in modes) for key in env):
        raise StoreError("UNSUPPORTED_CONSTRAINT", "Provider environment names require canonical uppercase to prevent case-insensitive platform overrides.")
    allowed_auth = {"sql_read": {"DATABASE_URI"}, "redis_read": {"REDIS_PWD"},
                    "source_read": {"GITLAB_TOKEN", "GITLAB_PAT", "GITLAB_PERSONAL_ACCESS_TOKEN", "GITLAB_API_TOKEN"},
                    "document_read": {"CONFLUENCE_API_TOKEN", "CONFLUENCE_PERSONAL_TOKEN"}, "aws_read": set()}
    if set(connection.get("auth_env", {})) - allowed_auth[adapter]:
        raise StoreError("UNSUPPORTED_CONSTRAINT", "Authentication references may supply credentials, not alternate endpoints or executable settings.")
    if adapter == "aws_read":
        if (any(key.startswith("AWS_") and key not in _AWS_ENV for key in env)
                or any(key in env for key in ("EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR"))
                or connection.get("auth_env")):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Infra endpoint, executable, or credential-provider overrides are unsupported.")
        for key, bound in (("AWS_API_MCP_PROFILE_NAME", env.get("AWS_PROFILE")), ("AWS_DEFAULT_REGION", env.get("AWS_REGION"))):
            if key in env and env[key] != bound:
                raise StoreError("UNSUPPORTED_CONSTRAINT", "The Infra provider profile and location must remain bound to the target.")
        if "READ_OPERATIONS_ONLY" in env and env["READ_OPERATIONS_ONLY"].lower() != "true":
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Infra read-only mode cannot be disabled.")
    if adapter == "sql_read" and any(key.startswith("PG") and key not in {"PGSSLMODE", "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY"} for key in (*env, *connection.get("auth_env", {}))):
        raise StoreError("UNSUPPORTED_CONSTRAINT", "DB service files, alternate endpoints, and session startup options are unsupported.")
    if adapter == "sql_read" and env.get("PGSSLMODE", "require") not in {"require", "verify-ca", "verify-full"}:
        raise StoreError("UNSUPPORTED_CONSTRAINT", "DB transport validation cannot be disabled through environment overrides.")
    if adapter == "redis_read" and "REDIS_URL" in {*env, *connection.get("auth_env", {})}:
        raise StoreError("UNSUPPORTED_CONSTRAINT", "Cache connection URLs cannot override the explicit storage-area binding.")
    if adapter == "source_read" and (env.get("GITLAB_AUTH", "token") != "token" or any(key.startswith("GITLAB_OAUTH_") for key in env)):
        raise StoreError("UNSUPPORTED_CONSTRAINT", "The local Source adapter uses an explicit token reference, not an alternate OAuth server.")
    if adapter == "document_read" and any("OAUTH" in key for key in env):
        raise StoreError("UNSUPPORTED_CONSTRAINT", "The local Document adapter uses an explicit token reference, not an alternate OAuth tenant.")
    for field in ("GITLAB_SSL_VERIFY", "CONFLUENCE_SSL_VERIFY"):
        if field in env and env[field].lower() not in {"true", "1"}:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Resource server certificate validation cannot be disabled.")
    command = Path(connection["command"]).stem
    expected = {"sql_read": "postgres-mcp", "redis_read": "redis-mcp-server", "source_read": "mcp-gitlab", "document_read": "mcp-atlassian", "aws_read": "awslabs.aws-api-mcp-server"}
    if command == expected.get(adapter):
        accepted = (["--access-mode=restricted"], ["--access-mode", "restricted"]) if adapter == "sql_read" else ([],)
        if connection.get("args", []) not in accepted:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "The prepared MCP launcher cannot override its registered connection or read-only mode.")
        if adapter in {"source_read", "document_read"} and not connection.get("auth_env"):
            raise StoreError("AUTHENTICATION_REQUIRED", "The prepared adapter needs an explicit credential reference; implicit provider credentials are not bound.")


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


def _sql_literal(node, path, types):
    if isinstance(node, ast.Expression):
        return _sql_literal(node.body, path, types)
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
        return node.value
    if isinstance(node, ast.List):
        return [_sql_literal(item, (*path, index), types) for index, item in enumerate(node.elts)]
    if isinstance(node, ast.Dict):
        value = {}
        for key, item in zip(node.keys, node.values):
            if not isinstance(key, ast.Constant) or type(key.value) is not str or key.value in value:
                raise ValueError
            value[key.value] = _sql_literal(item, (*path, key.value), types)
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _sql_literal(node.operand, path, types)
        if type(value) not in (int, float):
            raise ValueError
        return -value if isinstance(node.op, ast.USub) else value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Decimal"
            and not node.keywords and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is str and len(node.args[0].value) <= 1024):
        try:
            value = Decimal(node.args[0].value)
            if not value.is_finite() or abs(value.adjusted()) > 10000:
                raise ValueError
        except InvalidOperation:
            raise ValueError from None
        pointer = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)
        types[pointer] = "decimal"
        return format(value, "f")
    raise ValueError


def _legacy_result(result, adapter, *, unwrap=False, value_types=None):
    texts = [item.text for item in result.content if item.type == "text"]
    if len(texts) != len(result.content):
        raise StoreError("RESULT_VALIDATION_FAILED", "Only registered structured data or text results are supported.")
    if adapter in ("sql_read", "redis_read") and any(text.startswith(("Error:", "Error retrieving ")) for text in texts):
        raise StoreError("EXTERNAL_EXECUTION_FAILED", "The lower MCP reported an execution error in its text result.")
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError
            output[key] = value
        return output
    if result.structuredContent is not None:
        structured = result.structuredContent
        if (adapter in _NORMALIZED_ADAPTERS or unwrap) and isinstance(structured, dict) and set(structured) == {"result"}:
            payload = structured["result"]
            if isinstance(payload, list) and payload and all(isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) for item in payload):
                texts = [item["text"] for item in payload]
            elif isinstance(payload, str) and adapter in ("sql_read", "document_read", "source_read"):
                texts = [payload]
            else:
                return {"data": payload}
        else:
            return {"data": structured}
    if len(texts) != 1:
        return {"data": texts}
    text = texts[0]
    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except json.JSONDecodeError:
        value = text
        if adapter == "sql_read" and len(text) <= 262144 and text.lstrip().startswith(("[", "{")):
            try:
                tree = ast.parse(text, mode="eval")
                pending, count = [(tree, 0)], 0
                while pending:
                    node, depth = pending.pop()
                    count += 1
                    if count > 50000 or depth > 64:
                        raise ValueError
                    pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
                decimal_types = {}
                value = _sql_literal(tree, ("data",), decimal_types)
                def plain(item):
                    if type(item) in (str, int, float, bool, type(None)):
                        return
                    if type(item) is list:
                        for nested in item:
                            plain(nested)
                        return
                    if type(item) is dict and all(type(key) is str for key in item):
                        for nested in item.values():
                            plain(nested)
                        return
                    raise ValueError
                plain(value)
                _json(value)
                if value_types is not None:
                    value_types.update(decimal_types)
            except (ValueError, TypeError, SyntaxError, RecursionError, StoreError):
                value = text
    except (ValueError, RecursionError):
        raise StoreError("RESULT_VALIDATION_FAILED", "Ambiguous or non-finite JSON results are not supported.") from None
    return {"data": value}


def _normalized_output(config, tool):
    schema = tool.get("outputSchema")
    return (config["adapter"] in _NORMALIZED_ADAPTERS or schema is None
            or schema.get("x-fastmcp-wrap-result") is True)


def _output_schema(config, tool):
    schema = tool.get("outputSchema")
    if not _normalized_output(config, tool):
        return copy.deepcopy(schema)
    output = copy.deepcopy(_LEGACY_SCHEMA)
    if schema and config["adapter"] not in ("sql_read", "document_read"):
        if set(schema.get("properties", {})) == {"result"}:
            inner = copy.deepcopy(schema["properties"]["result"])
            if "$defs" in schema:
                inner["$defs"] = copy.deepcopy(schema["$defs"])
        else:
            inner = copy.deepcopy(schema)
        # Local $refs are rooted at the original schema; moving them would change
        # their meaning, so retain opaque value shape when references are present.
        if '"$ref"' not in _json(inner):
            output["properties"]["data"] = inner
    return output


def _provider_error(value, adapter):
    if adapter == "aws_read":
        # The selected lower server encodes CLI failures in per-command objects.
        data = value.get("data", value.get("result", value))
        entries = data if isinstance(data, list) else [data]
        if any(isinstance(entry, dict) and entry.get("error") for entry in entries):
            raise StoreError("EXTERNAL_EXECUTION_FAILED", "The lower Infra MCP reported a failed read operation.")


def _source_complete(value, invocation):
    data = value.get("data", value)
    if isinstance(data, dict):
        metadata = data.get("metadata", data)
        if any(metadata.get(field) for field in ("truncated", "has_more", "hasNextPage", "next_cursor", "nextToken", "NextToken", "next_page")):
            return False
        if invocation.resource["adapter"] == "source_read" and invocation.operation in ("gitlab_get_file", "get_repository_file"):
            arguments = invocation.prepared_request["arguments"]
            if any(arguments.get(field) is not None for field in ("start_line", "end_line", "limit")) or arguments.get("offset", 0) != 0:
                return False
            if metadata.get("truncated") is False:
                return True
    if invocation.resource["adapter"] == "redis_read" and invocation.operation == "get":
        return True
    if invocation.resource["adapter"] == "sql_read" and isinstance(data, list) and len(data) >= invocation.access.get("max_rows", 0):
        return False
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
    if config["adapter"] not in READ_TOOLS or not isinstance(config["target"], dict) or not config["target"]:
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
    _connection_controls(config)
    capabilities = config["capabilities"]
    if not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 128:
        _fail()
    ids = []
    for capability in capabilities:
        if (not isinstance(capability, dict) or set(capability) - {"capability_id", "tool_name", "description", "constraints"}
                or not {"capability_id", "tool_name", "constraints"} <= set(capability)
                or not isinstance(capability["capability_id"], str) or not _ID.fullmatch(capability["capability_id"])
                or capability["tool_name"] not in READ_TOOLS[config["adapter"]]):
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
        _connection_controls(config)
        connection, target = config["connection"], config["target"]
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
        env = connection.get("env", {})
        adapter = config["adapter"]
        provider_files = {}
        if connection["transport"] == "stdio":
            if adapter == "sql_read":
                ref = connection.get("auth_env", {}).get("DATABASE_URI")
                uri = urlsplit(credentials.get(ref, ""))
                query = parse_qsl(uri.query, keep_blank_values=True)
                if (uri.scheme not in ("postgres", "postgresql") or not uri.hostname or uri.fragment
                        or any(key != "sslmode" or value not in ("require", "verify-ca", "verify-full") for key, value in query)
                        or len(query) > 1):
                    raise StoreError("UNSUPPORTED_CONSTRAINT", "DB binding requires one explicit credential URI with no indirect connection parameters.")
                try:
                    actual = {"host": uri.hostname, "port": uri.port or 5432, "database": unquote(uri.path.lstrip("/"))}
                except ValueError:
                    raise StoreError("UNSUPPORTED_CONSTRAINT", "The DB credential has an invalid endpoint.") from None
                if not actual["database"]:
                    raise StoreError("UNSUPPORTED_CONSTRAINT", "The DB name must be explicit instead of a provider default.")
                if not all(key in target and target[key] == value for key, value in actual.items()):
                    raise StoreError("CONTEXT_CHANGED", "The DB credential resolves to a different endpoint or database.")
                if "username" in target and target["username"] != unquote(uri.username or ""):
                    raise StoreError("CONTEXT_CHANGED", "The DB login principal changed.")
            if adapter == "redis_read":
                try:
                    actual = {"host": env.get("REDIS_HOST"), "port": int(env.get("REDIS_PORT", "6379")), "db": int(env.get("REDIS_DB", "0"))}
                except ValueError:
                    raise StoreError("UNSUPPORTED_CONSTRAINT", "The Cache endpoint must have a numeric port and storage area.") from None
                if not all(key in target and target[key] == value for key, value in actual.items()):
                    raise StoreError("CONTEXT_CHANGED", "The Cache connection resolves to a different storage area.")
            if adapter in ("source_read", "document_read"):
                field = "GITLAB_URL" if adapter == "source_read" else "CONFLUENCE_URL"
                if not isinstance(target.get("server"), str) or not target["server"] or env.get(field, "").rstrip("/") != target["server"].rstrip("/"):
                    raise StoreError("CONTEXT_CHANGED", "The resource server differs from its configured target.")
            if adapter == "aws_read":
                profile_name = env.get("AWS_PROFILE")
                if not profile_name or target.get("profile") != profile_name or target.get("region") != env.get("AWS_REGION"):
                    raise StoreError("CONTEXT_CHANGED", "The Infra provider profile or region changed.")
                aws = Path.home() / ".aws"
                config_path = aws / "config"
                parser = configparser.ConfigParser(interpolation=None)
                try:
                    parser.read(config_path)
                    credential_settings = configparser.ConfigParser(interpolation=None)
                    credential_settings.read(aws / "credentials")
                    if parser.has_section("plugins") and list(parser["plugins"].items()):
                        raise ValueError
                    alias = aws / "cli" / "alias"
                    if alias.exists() and (alias.is_symlink() or alias.read_bytes().strip()):
                        raise ValueError
                    section = "default" if profile_name == "default" else "profile " + profile_name
                    settings = parser[section]
                    if settings.get("region", target["region"]) != target["region"]:
                        raise ValueError
                    visited, current = set(), profile_name
                    while current:
                        if current in visited or len(visited) >= 16:
                            raise ValueError
                        visited.add(current)
                        selected = "default" if current == "default" else "profile " + current
                        values = dict(parser[selected]) if parser.has_section(selected) else {}
                        if credential_settings.has_section(current):
                            values.update(credential_settings[current])
                        if any(key in values for key in ("credential_process", "credential_source", "web_identity_token_file", "login_session", "services")):
                            raise ValueError
                        if any("endpoint" in key or "endpoint_url" in value.lower() for key, value in values.items()):
                            raise ValueError
                        current = values.get("source_profile")
                    account = settings.get("sso_account_id")
                    role = settings.get("role_arn", "")
                    if account and role:
                        raise ValueError
                    if role:
                        match = re.fullmatch(r"arn:aws(?:-[a-z0-9-]+)?:iam::([0-9]{12}):role/[^\s]+", role)
                        if not match:
                            raise ValueError
                        account = match[1]
                    if not account or not re.fullmatch(r"[0-9]{12}", account) or target.get("account") != account:
                        raise ValueError
                    candidates = [config_path, aws / "credentials", *sorted((aws / "sso" / "cache").glob("*.json")), *sorted((aws / "cli" / "cache").glob("*.json"))]
                    if len(candidates) > 512:
                        raise ValueError
                    for path in candidates:
                        if path.is_symlink():
                            raise ValueError
                        if path.exists():
                            if path.stat().st_size > 2097152:
                                raise ValueError
                            provider_files[str(path.relative_to(aws))] = hashlib.sha256(path.read_bytes()).hexdigest()
                except (OSError, ValueError, KeyError, configparser.Error):
                    raise StoreError("UNSUPPORTED_CONSTRAINT", "Infra binding requires a configured SSO account or explicit role account matching the target.") from None
        return hmac.new(self.binding_key, _json([credentials, provider_files]).encode(), hashlib.sha256).hexdigest(), credentials

    def _resource(self, profile, instance_id):
        items = [resource for resource in profile.get("resources", []) if resource.get("instance_id") == instance_id]
        if len(items) != 1:
            raise StoreError("NOT_FOUND", "The resource is not registered in this profile.")
        return validate_resource(items[0])

    def _launch_connection(self, config):
        _connection_controls(config)
        connection = copy.deepcopy(config["connection"])
        if config["adapter"] == "aws_read" and connection["transport"] == "stdio":
            env = connection.setdefault("env", {})
            env.update({"AWS_API_MCP_PROFILE_NAME": env["AWS_PROFILE"], "AWS_DEFAULT_REGION": env["AWS_REGION"],
                        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true", "AWS_EC2_METADATA_DISABLED": "true",
                        "READ_OPERATIONS_ONLY": "true", "AWS_API_MCP_TELEMETRY": "false", "EXPERIMENTAL_AGENT_SCRIPTS": "false",
                        "AWS_API_MCP_ALLOW_UNRESTRICTED_LOCAL_FILE_ACCESS": "no-access"})
        if config["adapter"] == "redis_read" and connection["transport"] == "stdio" and Path(connection["command"]).stem == "redis-mcp-server":
            if connection.get("args"):
                raise StoreError("UNSUPPORTED_CONSTRAINT", "The prepared Cache launcher derives target flags from registered environment; target override arguments are unsupported.")
            env = connection.get("env", {})
            connection["args"] = ["--host", env.get("REDIS_HOST", ""), "--port", env.get("REDIS_PORT", "6379"), "--db", env.get("REDIS_DB", "0")]
            if env.get("REDIS_SSL", "").lower() in ("true", "1", "t"):
                connection["args"].append("--ssl")
            if env.get("REDIS_USERNAME"):
                connection["args"].extend(["--username", env["REDIS_USERNAME"]])
            if env.get("REDIS_CLUSTER_MODE", "").lower() in ("true", "1", "t"):
                connection["args"].append("--cluster-mode")
        return connection

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
                "input_schema": tool["inputSchema"], "output_schema": _output_schema(config, tool),
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
        if config["adapter"] == "source_read":
            for field in ("ref", "path"):
                if field in properties and (field not in arguments or arguments[field] is None):
                    raise StoreError("UNSUPPORTED_CONSTRAINT", "Source revision and path must be explicit; provider defaults cannot choose the approved target.")
        _validate(arguments, descriptor["input_schema"])
        access = normalize(config["adapter"], arguments, capability["constraints"], scope)
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
                    if _normalized_output(invocation.resource, invocation.tool_contract):
                        value = _legacy_result(result, invocation.resource["adapter"], unwrap=True, value_types=value_types)
                        schema = _output_schema(invocation.resource, invocation.tool_contract)
                    _provider_error(value, invocation.resource["adapter"])
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
        warnings = ["Live read; lower internal request count is not observable."]
        if invocation.resource["adapter"] == "sql_read" and isinstance(result.get("data"), str):
            warnings.append("The DB response is unstructured text; it is not certified row data for tables or calculations.")
        return {"result": result, "value_types": value_types, "units": {}, "warnings": warnings, "source_complete": _source_complete(result, invocation),
                "external_requests": None, "runtime_ms": max(0, int((monotonic() - started) * 1000))}
