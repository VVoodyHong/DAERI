"""Redis literal-key reads and explicit storage-area binding."""

from pathlib import Path

from daeri.infrastructure.adapters.base import ReadAdapter
from daeri.core.errors import StoreError


class RedisReadAdapter(ReadAdapter):
    adapter_id = "redis_read"
    resource_type = "Cache"
    read_tools = frozenset({
        # redis-mcp-server 0.5.1 has no read-only hints or mode. These definitions
        # only read Redis; subscription/group consumers and external docs search do not.
        'scan_keys', 'scan_all_keys', 'hexists', 'llen', 'get_vector_from_hash',
        'get_indexes', 'get_index_info', 'get_indexed_keys_number',
        'vector_search_hash', 'hybrid_search', 'dbsize', 'info', 'client_list', 'xrange',
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
    setup_fields = {
        "host": {"schema": {"title": "Cache 주소", "description": "접속할 Cache의 호스트 이름 또는 IP 주소입니다.",
                            "type": "string", "minLength": 1, "maxLength": 253,
                            "pattern": r"^[^\s/@?#\\\x00-\x1f\x7f]+$", "examples": ["cache.example.test"]},
                 "required": True, "paths": [["target", "host"], ["connection", "env", "REDIS_HOST"]]},
        "port": {"schema": {"title": "Cache 포트", "description": "Cache에 접속할 포트 번호입니다.",
                            "type": "integer", "minimum": 1, "maximum": 65535,
                            "default": 6379, "examples": [6379]},
                 "required": True, "paths": [["target", "port"], ["connection", "env", "REDIS_PORT"]]},
        "db": {"schema": {"title": "Cache 저장 영역", "description": "접속할 논리 데이터베이스의 번호입니다.",
                          "type": "integer", "minimum": 0, "maximum": 2147483647,
                          "default": 0, "examples": [0]},
               "required": True, "paths": [["target", "db"], ["connection", "env", "REDIS_DB"]]},
        "tls": {"schema": {"title": "Cache TLS 사용", "description": "암호화된 TLS 연결을 사용할지 선택합니다.",
                           "type": "boolean", "default": True, "examples": [True]},
                "required": True, "paths": [["connection", "env", "REDIS_SSL"]]},
        "username": {"schema": {"title": "Cache 사용자 이름", "description": "사용자 이름 인증을 사용하는 경우 계정 이름을 지정합니다.",
                                "type": "string", "minLength": 1, "maxLength": 128,
                                "pattern": r"^[^\x00-\x1f\x7f]+$", "examples": ["readonly_user"]},
                     "required": False, "paths": [["connection", "env", "REDIS_USERNAME"]]},
    }
    environment_prefixes = ("REDIS_",)
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    authentication_fields = frozenset({"REDIS_PWD"})
    setup_credentials = {'REDIS_PWD': {'field': 'password', 'description': '캐시 접속에 사용할 비밀번호입니다.'}}
    executable = "redis-mcp-server"
    normalize_results = True
    text_error_prefixes = ("Error:", "Error retrieving ", "Error getting ", "Error scanning ",
                           "Error checking ", "Error performing ")

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
                connection["args"].extend(["--ssl", "--ssl-cert-reqs", "none"])
            if env.get("REDIS_USERNAME"):
                connection["args"].extend(["--username", env["REDIS_USERNAME"]])
            if env.get("REDIS_CLUSTER_MODE", "").lower() in ("true", "1", "t"):
                connection["args"].append("--cluster-mode")
        return connection

    def provider_error(self, value):
        data = value.get('data', value)
        if isinstance(data, dict) and data.get('error'):
            raise StoreError('EXTERNAL_EXECUTION_FAILED', 'The lower Cache MCP reported a failed read operation.')
