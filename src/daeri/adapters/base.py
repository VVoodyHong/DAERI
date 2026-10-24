"""Trusted read-adapter contract and product-independent request/result checks."""

import copy
import json
import math
from pathlib import Path, PurePosixPath

from ..storage import StoreError


def denied(message="The actual request is outside the approved scope."):
    raise StoreError("SCOPE_VIOLATION", message)

def clean_path(value):
    if (not isinstance(value, str) or not value or value.startswith(("/", "\\"))
            or any(char in value for char in ("\\", "%", "\x00", ":", "?", "#"))
            or any(part in ("", ".", "..") for part in value.split("/"))):
        denied("An unambiguous relative path is required.")
    return str(PurePosixPath(value))

def _prefix(value, prefix):
    value, prefix = clean_path(value), clean_path(prefix)
    return value == prefix or value.startswith(prefix + "/")

def _allowed(value, allowed):
    if isinstance(value, list):
        return all(any(type(v) is type(a) and v == a for a in allowed) for v in value)
    return any(type(value) is type(item) and value == item for item in allowed)

def _guard(parameters, guards, *, cover=False):
    covered = set()
    for field, expected in guards.get("equals", guards.get("fixed", {})).items():
        if field not in parameters or type(parameters[field]) is not type(expected) or parameters[field] != expected:
            denied()
        covered.add(field)
    for field, allowed in guards.get("one_of", guards.get("allowed_values", {})).items():
        if field in parameters:
            if not _allowed(parameters[field], allowed):
                denied()
            covered.add(field)
    for field, prefixes in guards.get("path_prefixes", {}).items():
        if field in parameters:
            if not any(_prefix(parameters[field], prefix) for prefix in prefixes):
                denied()
            covered.add(field)
    for field, maximum in guards.get("max_values", {}).items():
        if field in parameters:
            value = parameters[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= maximum:
                denied()
            covered.add(field)
    if cover and set(parameters) - covered:
        raise StoreError("UNSUPPORTED_CONSTRAINT", "Every transmitted argument needs an explicit approved field constraint.")


def json_text(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError):
        raise StoreError("INVALID_INPUT", "A bounded JSON value is required.") from None


class ReadAdapter:
    """Only trusted registered subclasses may interpret an external read tool."""

    adapter_id = ""
    aliases = ()
    resource_type = ""
    read_tools = frozenset()
    target_template = {}
    setup_fields = {}
    setup_credentials = {}
    environment_prefixes = ()
    control_fields = frozenset()
    authentication_fields = frozenset()
    executable = None
    launcher_arguments = ((),)
    explicit_credentials = False
    normalize_results = False
    decode_result_strings = False
    preserve_inner_schema = True
    text_error_prefixes = ()
    login_recipe_id = None
    login_helper = None

    def normalize_setup_values(self, values):
        """Return validated setup inputs in the adapter's stored representation."""
        return values.copy()

    def finalize_setup_resource(self, resource, *, profile_name):
        """Finalize adapter-owned public/authentication fields after setup binding."""
        return resource

    def validate_connection(self, config):
        connection = config["connection"]
        if connection["transport"] != "stdio":
            raise StoreError("UNSUPPORTED_CONSTRAINT", "This adapter cannot verify the remote MCP resource target and authentication context; use its supported local connection.")
        env = connection.get("env", {})
        forbidden = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS", "BASH_ENV", "ENV", "SHELLOPTS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
        if any(key.upper() in forbidden or key.upper().startswith(("DYLD_", "LD_")) for key in (*env, *connection.get("auth_env", {}))):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Program injection and implicit proxy environment overrides are unsupported.")
        if any(key != key.upper() and (key.upper().startswith(self.environment_prefixes) or key.upper() in self.control_fields) for key in env):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Provider environment names require canonical uppercase to prevent case-insensitive platform overrides.")
        if set(connection.get("auth_env", {})) - self.authentication_fields:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Authentication references may supply credentials, not alternate endpoints or executable settings.")
        for field, value in env.items():
            if field.endswith("_SSL_VERIFY") and value.lower() not in {"true", "1"}:
                raise StoreError("UNSUPPORTED_CONSTRAINT", "Resource server certificate validation cannot be disabled.")
        if Path(connection["command"]).stem == self.executable:
            if tuple(connection.get("args", [])) not in self.launcher_arguments:
                raise StoreError("UNSUPPORTED_CONSTRAINT", "The prepared MCP launcher cannot override its registered connection or read-only mode.")
            if self.explicit_credentials and not connection.get("auth_env"):
                raise StoreError("AUTHENTICATION_REQUIRED", "The prepared adapter needs an explicit credential reference; implicit provider credentials are not bound.")

    def binding_files(self, config, credentials):
        return {}

    def launch_connection(self, config):
        self.validate_connection(config)
        return copy.deepcopy(config["connection"])

    def session_environment(self, config, environment):
        """Prepare resolved process values at the private transport boundary."""
        return environment.copy()

    def prepare_parameters(self, parameters, input_schema):
        pass

    def normalize(self, parameters, constraints, scope):
        _guard(parameters, constraints)
        _guard(parameters, scope, cover=True)
        return {"fields": sorted(parameters)}

    def normalized_output(self, tool):
        schema = tool.get("outputSchema")
        return self.normalize_results or schema is None or schema.get("x-fastmcp-wrap-result") is True

    def output_schema(self, tool):
        schema = tool.get("outputSchema")
        if not self.normalized_output(tool):
            return copy.deepcopy(schema)
        output = {"type": "object", "properties": {"data": {}}, "required": ["data"], "additionalProperties": False}
        if schema and self.preserve_inner_schema:
            if set(schema.get("properties", {})) == {"result"}:
                inner = copy.deepcopy(schema["properties"]["result"])
                if "$defs" in schema:
                    inner["$defs"] = copy.deepcopy(schema["$defs"])
            else:
                inner = copy.deepcopy(schema)
            # Moving a root-relative reference would change the original schema.
            if '"$ref"' not in json_text(inner):
                output["properties"]["data"] = inner
        return output

    def legacy_text(self, text, value_types):
        return text

    def legacy_result(self, result, *, unwrap=False, value_types=None):
        texts = [item.text for item in result.content if item.type == "text"]
        if len(texts) != len(result.content):
            raise StoreError("RESULT_VALIDATION_FAILED", "Only registered structured data or text results are supported.")
        if self.text_error_prefixes and any(text.startswith(self.text_error_prefixes) for text in texts):
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
            if (self.normalize_results or unwrap) and isinstance(structured, dict) and set(structured) == {"result"}:
                payload = structured["result"]
                if isinstance(payload, list) and payload and all(isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) for item in payload):
                    texts = [item["text"] for item in payload]
                elif isinstance(payload, str) and self.decode_result_strings:
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
            value = self.legacy_text(text, value_types)
        except (ValueError, RecursionError):
            raise StoreError("RESULT_VALIDATION_FAILED", "Ambiguous or non-finite JSON results are not supported.") from None
        return {"data": value}

    def decode_result(self, result, tool, value_types):
        if self.normalized_output(tool):
            return self.legacy_result(result, unwrap=True, value_types=value_types), self.output_schema(tool)
        return result.structuredContent, tool.get("outputSchema")

    def provider_error(self, value):
        pass

    def source_complete(self, value, invocation):
        data = value.get("data", value)
        if isinstance(data, dict):
            metadata = data.get("metadata", data)
            if isinstance(metadata, dict) and any(metadata.get(field) for field in ("truncated", "has_more", "hasNextPage", "next_cursor", "nextToken", "NextToken", "next_page")):
                return False
        return None

    def warnings(self, value):
        return ["Live read; lower internal request count is not observable."]

    def login_request(self, resource, **options):
        raise StoreError("INVALID_INPUT", "This resource does not provide the selected login flow.")

    def validate_login_prompt(self, data, resource):
        raise StoreError("INVALID_INPUT", "This resource does not provide the selected login flow.")

    def validate_login_result(self, data, resource):
        raise StoreError("INVALID_INPUT", "This resource does not provide the selected login flow.")
