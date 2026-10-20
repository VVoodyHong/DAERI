"""PostgreSQL read connection, SQL scope, and typed legacy result interpretation."""

import ast
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, unquote, urlsplit

from .base import ReadAdapter, _guard, denied, json_text as _json
from ..storage import StoreError


def _sql(parameters, constraints, scope):
    import sqlglot
    from sqlglot import exp

    field = constraints.get("sql_field", "sql")
    query = parameters.get(field)
    if not isinstance(query, str) or len(query) > 32768:
        denied()
    _guard(parameters, constraints)
    _guard(parameters, scope)
    try:
        statements = sqlglot.parse(query, read="postgres")
    except sqlglot.errors.SqlglotError:
        raise StoreError("UNSUPPORTED_CONSTRAINT", "The SQL request could not be interpreted.") from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        denied("Only one SELECT statement is supported.")
    tree = statements[0]
    if (len(list(tree.find_all(exp.Select))) != 1 or tree.args.get("with_")
            or tree.args.get("into") or tree.args.get("locks")
            or tree.find(exp.Command) or tree.find(exp.Parameter)):
        denied("Indirect SQL, locking, and procedural expressions are unsupported.")
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tree.args.get("joins") or not tables[0].db or tables[0].catalog:
        raise StoreError("UNSUPPORTED_CONSTRAINT", "Use one explicitly schema-qualified base table.")
    table = tables[0]
    target = f"{table.db}.{table.name}"
    if target not in constraints.get("tables", []) or target not in scope.get("tables", []):
        denied()
    columns = sorted({column.name for column in tree.find_all(exp.Column) if column.name != "*"})
    if not set(columns) <= set(constraints.get("columns", [])) or not set(columns) <= set(scope.get("columns", [])):
        denied()
    for star in tree.find_all(exp.Star):
        if not isinstance(star.parent, exp.Count):
            denied("Wildcard field reads are unsupported.")
    permitted_functions = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)
    if any(not isinstance(function, permitted_functions) for function in tree.find_all(exp.Func)):
        denied("SQL functions outside the verified aggregate set are unsupported.")
    if any(column.table and column.table not in (table.name, table.alias) for column in tree.find_all(exp.Column)):
        denied()
    maximum = min(constraints.get("max_rows", 0), scope.get("max_rows", 0))
    if type(maximum) is not int or maximum <= 0:
        raise StoreError("UNSUPPORTED_CONSTRAINT", "An enforceable SQL row limit is required.")
    limit = tree.args.get("limit")
    if limit:
        literal = limit.expression
        if not isinstance(literal, exp.Literal) or literal.is_string or not literal.this.isdigit():
            denied()
        maximum = min(maximum, int(literal.this))
    parameters[field] = tree.limit(maximum).sql(dialect="postgres")
    others = {key: value for key, value in parameters.items() if key != field}
    other_scope = {key: ({name: value for name, value in values.items() if name != field}
                         if isinstance(values, dict) else values) for key, values in scope.items()}
    _guard(others, other_scope, cover=True)
    return {"tables": [target], "columns": columns, "max_rows": maximum}


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
    })
    target_template = {"host": "SET-ME", "port": 5432, "database": "SET-ME", "username": "SET-ME"}
    environment_prefixes = ("PG",)
    control_fields = frozenset({
        "READ_OPERATIONS_ONLY", "MCP_READ_ONLY", "READ_ONLY_MODE", "ENABLED_TOOLS",
        "EXPERIMENTAL_AGENT_SCRIPTS", "ENDPOINT_SUGGEST_AWS_COMMANDS", "CUSTOM_SCRIPTS_DIR",
    })
    authentication_fields = frozenset({"DATABASE_URI"})
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
        if any(key.startswith("PG") and key not in {"PGSSLMODE", "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY"} for key in (*env, *connection.get("auth_env", {}))):
            raise StoreError("UNSUPPORTED_CONSTRAINT", "DB service files, alternate endpoints, and session startup options are unsupported.")
        if env.get("PGSSLMODE", "require") not in {"require", "verify-ca", "verify-full"}:
            raise StoreError("UNSUPPORTED_CONSTRAINT", "DB transport validation cannot be disabled through environment overrides.")

    def binding_files(self, config, credentials):
        connection, target = config["connection"], config["target"]
        if connection["transport"] != "stdio":
            return {}
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
        return {}

    def normalize(self, parameters, constraints, scope):
        return _sql(parameters, constraints, scope)

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

    def source_complete(self, value, invocation):
        if super().source_complete(value, invocation) is False:
            return False
        data = value.get("data", value)
        if isinstance(data, list) and len(data) >= invocation.access.get("max_rows", 0):
            return False
        return None

    def warnings(self, value):
        result = super().warnings(value)
        if isinstance(value.get("data"), str):
            result.append("The DB response is unstructured text; it is not certified row data for tables or calculations.")
        return result
