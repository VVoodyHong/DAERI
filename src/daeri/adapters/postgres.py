"""PostgreSQL read connection, SQL scope, and typed legacy result interpretation."""

import ast
import ipaddress
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from .base import ReadAdapter, json_text as _json
from ..storage import StoreError


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


class PostgresReadAdapter(ReadAdapter):
    adapter_id = "postgres_read"
    aliases = ("sql_read",)
    resource_type = "DB"
    read_tools = frozenset({
        'execute_sql',
        'query',
        'read_query',
        # postgres-mcp 0.3.0 routes these through its restricted SQL driver.
        'list_schemas', 'list_objects', 'get_object_details', 'explain_query',
        'analyze_db_health', 'get_top_queries',
    })
    target_template = {"host": "SET-ME", "port": 5432, "database": "SET-ME", "username": "SET-ME"}
    setup_fields = {
        "host": {"schema": {"title": "DB 주소", "description": "접속할 DB의 호스트 이름 또는 IP 주소입니다.",
                            "type": "string", "minLength": 1, "maxLength": 253,
                            "pattern": r"^[^\s/@?#\\\x00-\x1f\x7f]+$", "examples": ["db.example.test"]},
                 "required": True, "paths": [["target", "host"]]},
        "port": {"schema": {"title": "DB 포트", "description": "DB에 접속할 포트 번호입니다.",
                            "type": "integer", "minimum": 1, "maximum": 65535,
                            "default": 5432, "examples": [5432]},
                 "required": True, "paths": [["target", "port"]]},
        "database": {"schema": {"title": "데이터베이스 이름", "description": "조회할 데이터베이스의 정확한 이름입니다.",
                                "type": "string", "minLength": 1, "maxLength": 128,
                                "pattern": r"^[^\x00-\x1f\x7f]+$", "examples": ["service_db"]},
                     "required": True, "paths": [["target", "database"]]},
        "username": {"schema": {"title": "DB 사용자 이름", "description": "연결에 사용할 계정 이름입니다.",
                                "type": "string", "minLength": 1, "maxLength": 128,
                                "pattern": r"^[^\x00-\x1f\x7f]+$", "examples": ["readonly_user"]},
                     "required": True, "paths": [["target", "username"]]},
    }
    environment_prefixes = ("PG",)
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    authentication_fields = frozenset({"DATABASE_PASSWORD", "DATABASE_URI"})
    setup_credentials = {
        'DATABASE_PASSWORD': {'field': 'password', 'description': 'DB 접속에 사용할 비밀번호입니다.'},
        'DATABASE_URI': {'field': 'uri', 'description': '기존 URI 인증 연결에 사용할 전체 DB 접속 URI입니다.'},
    }
    executable = "postgres-mcp"
    launcher_arguments = (("--access-mode=restricted",), ("--access-mode", "restricted"))
    normalize_results = True
    decode_result_strings = True
    preserve_inner_schema = False
    text_error_prefixes = ("Error:", "Error retrieving ")

    def validate_connection(self, config):
        super().validate_connection(config)
        connection = config["connection"]
        if connection["transport"] != "stdio":
            return
        env = connection.get("env", {})
        if len(set(connection.get("auth_env", {})) & self.authentication_fields) > 1:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "Select one DB password or legacy URI authentication reference.")
        if any(key.startswith("PG") and key not in {"PGSSLMODE", "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY"} for key in (*env, *connection.get("auth_env", {}))):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "DB service files, alternate endpoints, and session startup options are unsupported.")
        if env.get("PGSSLMODE", "require") not in {"require", "verify-ca", "verify-full"}:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "DB transport validation cannot be disabled through environment overrides.")

    def _password_uri(self, config, password):
        target = config["target"]
        for field in ("host", "database", "username"):
            schema = self.setup_fields[field]["schema"]
            value = target.get(field)
            if (not isinstance(value, str) or not schema["minLength"] <= len(value) <= schema["maxLength"]
                    or not re.fullmatch(schema["pattern"], value)):
                raise StoreError("UNSUPPORTED_CONSTRAINT", "The DB password requires an explicit valid target and login principal.")
        host, port = target["host"], target.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "The DB target requires one numeric port.")
        if any(char in host for char in ",%"):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "The DB target requires one explicit host.")
        if ":" in host or "[" in host or "]" in host:
            try:
                address = ipaddress.IPv6Address(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
            except ValueError:
                raise StoreError("UNSUPPORTED_CONSTRAINT", "The DB target has an invalid IPv6 host.") from None
            host = "[" + str(address) + "]"
        mode = config["connection"].get("env", {}).get("PGSSLMODE", "require")
        return ("postgresql://" + quote(target["username"], safe="") + ":" + quote(password, safe="")
                + "@" + host + ":" + str(port) + "/" + quote(target["database"], safe="") + "?sslmode=" + mode)

    def session_environment(self, config, environment):
        environment = super().session_environment(config, environment)
        if "DATABASE_PASSWORD" in config["connection"].get("auth_env", {}):
            environment["DATABASE_URI"] = self._password_uri(config, environment.pop("DATABASE_PASSWORD"))
        return environment

    def binding_files(self, config, credentials):
        connection, target = config["connection"], config["target"]
        if connection["transport"] != "stdio":
            return {}
        if "DATABASE_PASSWORD" in connection.get("auth_env", {}):
            # Target validation also runs before discovery and every approved call.
            self._password_uri(config, "binding-check")
            return {}
        ref = connection.get("auth_env", {}).get("DATABASE_URI")
        try:
            uri = urlsplit(credentials.get(ref, ""))
        except ValueError:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "The DB credential has an invalid endpoint.") from None
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
        return {}

    def normalize(self, parameters, constraints, scope):
        if any(set(guards) & {'sql_field', 'tables', 'columns', 'max_rows'} for guards in (constraints, scope)):
            raise StoreError('UNSUPPORTED_CONSTRAINT',
                             'Legacy SQL semantic limits require an explicit resource contract refresh and a new plan; use DB permissions or supported field constraints.')
        return super().normalize(parameters, constraints, scope)

    def legacy_text(self, text, value_types):
        if len(text) > 262144 or not text.lstrip().startswith(("[", "{")):
            return text
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
            _json(value)
            if value_types is not None:
                value_types.update(decimal_types)
            return value
        except (ValueError, TypeError, SyntaxError, RecursionError, StoreError):
            return text

    def warnings(self, value):
        result = super().warnings(value)
        if isinstance(value.get("data"), str):
            result.append("The DB response is unstructured text; it is not certified row data for tables or calculations.")
        return result
