"""Local request interpreters; these modules never connect to resources."""

import math
import re
import shlex
from pathlib import PurePosixPath

from ..storage import StoreError

READ_TOOLS = {
    "source_read": {"get_repository_file", "list_repository_tree", "get_file_contents", "get_file_content", "list_branches", "get_commit", "get_project", "get_merge_request", "list_merge_requests", "get_merge_request_diffs", "gitlab_get_file", "gitlab_list_tree", "gitlab_get_project", "gitlab_list_branches", "gitlab_get_commit", "gitlab_get_mr", "gitlab_list_mrs", "gitlab_mr_changes"},
    "document_read": {"confluence_get_page", "confluence_get_page_children", "confluence_get_page_history", "confluence_search", "getConfluencePage", "getConfluencePageDescendants", "getConfluenceSpaces", "getPagesInConfluenceSpace"},
    "redis_read": {"get", "string_get", "get_string", "hget", "hash_get", "hgetall", "hash_get_all", "ttl", "type", "lrange", "list_range", "smembers", "set_members", "zrange", "sorted_set_range", "json_get"},
    "sql_read": {"execute_sql", "query", "read_query"},
    "aws_read": {"call_aws", "aws_api_call", "call_aws_api"},
}


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


AWS_READ = {
    ("ec2", "describe-instances"), ("ec2", "describe-security-groups"),
    ("ec2", "describe-vpcs"), ("ec2", "describe-subnets"), ("ec2", "describe-volumes"),
    ("rds", "describe-db-instances"), ("rds", "describe-db-clusters"),
    ("logs", "describe-log-groups"), ("cloudwatch", "list-metrics"), ("sts", "get-caller-identity"),
}


def _aws(parameters, constraints, scope):
    field = constraints.get("command_field", "cli_command")
    command = parameters.get(field)
    if not isinstance(command, str) or any(char in command for char in "\n\r;|&`$<>\\"):
        denied("Only a parsed, approved read operation is supported.")
    try:
        tokens = shlex.split(command)
    except ValueError:
        denied()
    if len(tokens) < 3 or tokens[0] != "aws" or tuple(tokens[1:3]) not in AWS_READ:
        denied()
    if any(any(scheme in value.lower() for scheme in ("file:", "http:", "https:")) for value in tokens):
        denied()
    forbidden = {"--endpoint-url", "--profile", "--debug", "--cli-input-json", "--cli-input-yaml", "--generate-cli-skeleton", "--output", "--query", "--no-verify-ssl", "--ca-bundle"}
    flags = [token.split("=", 1)[0] for token in tokens[3:] if token.startswith("--")]
    if any(any(option.startswith(flag) for option in forbidden) for flag in flags):
        denied()
    if any("--region".startswith(flag) and flag != "--region" for flag in flags) or flags.count("--region") != 1:
        denied()
    region = constraints.get("region")
    if not isinstance(region, str) or tokens.count("--region") != 1:
        denied("An explicit, configured Infra region is required.")
    index = tokens.index("--region")
    if index + 1 >= len(tokens) or tokens[index + 1] != region:
        denied()
    _guard(parameters, constraints)
    _guard(parameters, scope, cover=True)
    return {"service": tokens[1], "operation": tokens[2], "region": region}


def normalize(adapter, parameters, constraints, scope):
    if adapter == "sql_read":
        return _sql(parameters, constraints, scope)
    if adapter == "aws_read":
        return _aws(parameters, constraints, scope)
    _guard(parameters, constraints)
    _guard(parameters, scope, cover=True)
    if adapter == "redis_read":
        keys = [parameters[field] for field in ("key", "name") if field in parameters]
        if not keys or any(not isinstance(key, str) or any(c in key for c in "*?[]\x00") for key in keys):
            denied("Cache reads require explicit literal keys.")
    if adapter == "source_read":
        for field in ("project_id", "id"):
            if field in parameters and isinstance(parameters[field], str):
                clean_path(parameters[field])
        for field in ("file_path", "path"):
            if field in parameters:
                clean_path(parameters[field])
        if "url" in parameters:
            denied("Use explicit repository/ref/path arguments instead of an indirect URL.")
    if adapter == "document_read" and any(field in parameters for field in ("query", "cql", "url")):
        raise StoreError("UNSUPPORTED_CONSTRAINT", "Unparsed Document query languages and indirect URLs are unsupported; use page/space IDs.")
    if adapter == "document_read" and "page_id" in parameters and (not isinstance(parameters["page_id"], str) or not parameters["page_id"].isdigit()):
        denied("Use an explicit numeric page ID instead of an indirect URL or tiny link.")
    return {"fields": sorted(parameters)}
