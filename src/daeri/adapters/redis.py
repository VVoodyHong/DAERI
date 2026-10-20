"""Redis literal-key reads and explicit storage-area binding."""

from pathlib import Path

from .base import ReadAdapter, denied
from ..storage import StoreError


class RedisReadAdapter(ReadAdapter):
    adapter_id = "redis_read"
    resource_type = "Cache"
    read_tools = frozenset({
        'get',
        'get_string',
        'hash_get',
        'hash_get_all',
        'hget',
        'hgetall',
        'json_get',
        'list_range',
        'lrange',
        'set_members',
        'smembers',
        'sorted_set_range',
        'string_get',
        'ttl',
        'type',
        'zrange',
    })
    target_template = {"host": "SET-ME", "port": 6379, "db": 0}
    environment_prefixes = ("REDIS_",)
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    authentication_fields = frozenset({"REDIS_PWD"})
    executable = "redis-mcp-server"
    normalize_results = True
    text_error_prefixes = ("Error:", "Error retrieving ")

    def validate_connection(self, config):
        super().validate_connection(config)
        connection = config["connection"]
        if connection["transport"] == "stdio" and "REDIS_URL" in {*connection.get("env", {}), *connection.get("auth_env", {})}:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Cache connection URLs cannot override the explicit storage-area binding.")

    def binding_files(self, config, credentials):
        connection, target = config["connection"], config["target"]
        if connection["transport"] != "stdio":
            return {}
        env = connection.get("env", {})
        try:
            actual = {"host": env.get("REDIS_HOST"), "port": int(env.get("REDIS_PORT", "6379")), "db": int(env.get("REDIS_DB", "0"))}
        except ValueError:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "The Cache endpoint must have a numeric port and storage area.") from None
        if not all(key in target and target[key] == value for key, value in actual.items()):
            raise StoreError("CONTEXT_CHANGED", "The Cache connection resolves to a different storage area.")
        return {}

    def launch_connection(self, config):
        connection = super().launch_connection(config)
        if connection["transport"] == "stdio" and Path(connection["command"]).stem == self.executable:
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

    def normalize(self, parameters, constraints, scope):
        result = super().normalize(parameters, constraints, scope)
        keys = [parameters[field] for field in ("key", "name") if field in parameters]
        if not keys or any(not isinstance(key, str) or any(c in key for c in "*?[]\x00") for key in keys):
            denied("Cache reads require explicit literal keys.")
        return result

    def source_complete(self, value, invocation):
        if super().source_complete(value, invocation) is False:
            return False
        return True if invocation.operation == "get" else None
