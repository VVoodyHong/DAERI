"""Actual lower MCP transport and fail-closed scope contract tests."""

import copy
import json
import sys
from pathlib import Path

import pytest

from daeri.resources import ResourceRegistry, validate_resource
from daeri.storage import Store, StoreError
from daeri.adapters import get_adapter, normalize as interpret


def config():
    return {"instance_id": "source-one", "type": "Source", "adapter": "source_read",
            "target": {"server": "https://git.example.test", "project": "team/service"},
            "identity_ref": "identity:reader", "connection": {"transport": "stdio", "command": sys.executable,
            "args": [str(Path(__file__).parent / "fixtures" / "resource_mcp.py")], "env": {"GITLAB_URL": "https://git.example.test"}},
            "capabilities": [{"capability_id": "read-file", "tool_name": "get_repository_file",
                "constraints": {"fixed": {"project_id": "team/service"},
                                "allowed_values": {"ref": ["main"]},
                                "path_prefixes": {"file_path": ["src"]}, "max_values": {"limit": 100}}}]}


@pytest.fixture
def registry(tmp_path):
    store = Store(tmp_path / "private")
    registry = ResourceRegistry(store)
    yield registry
    store.close()


def profile(resource=None):
    return {"profile_id": "profile_one", "revision": 1, "resources": [resource or config()]}


def scope():
    return {"equals": {"project_id": "team/service", "ref": "main"},
            "path_prefixes": {"file_path": ["src"]}, "max_values": {"limit": 50}}


def test_diagnosis_registers_only_selected_tool_and_actual_call(registry):
    p = profile()
    registry.diagnose(p, "source-one")
    descriptor = registry.get(p, "source-one", "read-file")
    assert descriptor["effects"]["changes_state"] is False
    invocation = registry.normalize(p, "source-one", "read-file", {"ref": "main", "file_path": "src/main.py", "limit": 20}, scope())
    assert invocation.prepared_request["arguments"]["project_id"] == "team/service"
    result = registry.execute(invocation)
    assert "team/service" in result["result"]["body"]
    assert "remove-this" not in str(result)
    assert result["external_requests"] is None


@pytest.mark.parametrize("parameters", [
    {"ref": "other", "file_path": "src/main.py"},
    {"ref": "main", "file_path": "src/../private/config"},
    {"ref": "main", "file_path": "src-other/main.py"},
    {"ref": "main", "file_path": "src/%2e%2e/private"},
    {"ref": "main", "file_path": "src/main.py", "limit": 51},
    {"project_id": "other/service", "ref": "main", "file_path": "src/main.py"},
    {"ref": "main", "file_path": "src/main.py", "url": "https://other.test"},
])
def test_scope_escape_never_dispatches(registry, parameters):
    p = profile()
    registry.diagnose(p, "source-one")
    with pytest.raises(StoreError):
        registry.normalize(p, "source-one", "read-file", parameters, scope())


def test_cold_registration_does_not_connect_implicitly(registry):
    with pytest.raises(StoreError, match="CONNECTION_NOT_READY"):
        registry.normalize(profile(), "source-one", "read-file", {"ref": "main", "file_path": "src/main.py"}, scope())


def test_scope_must_cover_every_transmitted_field(registry):
    p = profile()
    registry.diagnose(p, "source-one")
    narrow = scope()
    del narrow["path_prefixes"]
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        registry.normalize(p, "source-one", "read-file", {"ref": "main", "file_path": "src/main.py"}, narrow)


@pytest.mark.parametrize('origin', ['fixed', 'default'])
@pytest.mark.parametrize('character,count,suffix,accepted', [
    pytest.param('x', 65509, '', True, id='ascii-limit'),
    pytest.param('x', 65510, '', False, id='ascii-over'),
    pytest.param('가', 21836, 'x', True, id='utf8-limit'),
    pytest.param('가', 21836, 'xx', False, id='utf8-over'),
])
def test_normalized_argument_limit_covers_fixed_and_default_values(registry, tmp_path, origin,
                                                                 character, count, suffix, accepted):
    padding = character * count + suffix
    script = tmp_path / 'argument_limit_mcp.py'
    default = padding if origin == 'default' else ''
    script.write_text('from mcp.server.fastmcp import FastMCP\n'
                      'server = FastMCP("argument-limit-fixture")\n'
                      '@server.tool(name="get")\n'
                      f'def read(key: str, padding: str = {default!r}) -> dict:\n'
                      '    return {"padding_bytes": len(padding.encode("utf-8"))}\n'
                      'server.run()\n', encoding='utf-8')
    resource = config()
    resource.update(instance_id='cache', type='Cache', adapter='redis_read',
                    target={'host': 'cache.example.test', 'port': 6379, 'db': 0})
    resource['connection'].update(args=['-I', str(script)], env={
        'REDIS_HOST': 'cache.example.test', 'REDIS_PORT': '6379', 'REDIS_DB': '0'})
    resource['capabilities'] = [{'capability_id': 'read', 'tool_name': 'get',
        'constraints': {'fixed': {'padding': padding}} if origin == 'fixed' else {}}]
    p = profile(resource)
    registry.diagnose(p, 'cache')
    approved = {'equals': {'key': 'item', 'padding': padding}}
    if not accepted:
        with pytest.raises(StoreError, match='INVALID_INPUT'):
            registry.normalize(p, 'cache', 'read', {'key': 'item'}, approved)
        return
    invocation = registry.normalize(p, 'cache', 'read', {'key': 'item'}, approved)
    assert registry.execute(invocation)['result'] == {'data': {'padding_bytes': 65509}}


def test_connection_only_source_recipe_requires_approved_project_revision_and_path(registry):
    from daeri.setup import build_setup_resource
    resource = build_setup_resource(registry.store, 'source-gitlab', 'service', 'source-one',
                                    {'server': 'https://git.example.test'})
    assert resource['capabilities'][0]['constraints'] == {}
    # Use the isolated fixture's executable and tool; retain the recipe's access constraints.
    resource['connection'].update(command=sys.executable,
                                  args=config()['connection']['args'], auth_env={})
    resource['capabilities'][0]['tool_name'] = 'get_repository_file'
    p = profile(resource)
    parameters = {'project_id': 'team/service', 'ref': 'main', 'file_path': 'src/main.py'}
    with pytest.raises(StoreError, match='CONNECTION_NOT_READY'):
        registry.normalize(p, 'source-one', 'read-file', parameters, scope())
    registry.diagnose(p, 'source-one')
    invocation = registry.normalize(p, 'source-one', 'read-file', parameters, scope())
    assert invocation.prepared_request['arguments'] == parameters
    assert 'team/service' in registry.execute(invocation)['result']['body']
    for field, value in [('project_id', 'other/service'), ('ref', 'other'),
                         ('file_path', 'private/config')]:
        with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
            registry.normalize(p, 'source-one', 'read-file', parameters | {field: value}, scope())
    missing = scope()
    del missing['equals']['project_id']
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        registry.normalize(p, 'source-one', 'read-file', parameters, missing)


@pytest.mark.parametrize('recipe,inputs,allowed,denied,approved', [
    ('db-postgres', {'host': 'db.example.test', 'port': 5432, 'database': 'service', 'username': 'reader'},
     {'sql': 'SELECT order_id FROM public.orders'}, {'sql': 'SELECT password FROM private.users'},
     {'tables': ['public.orders'], 'columns': ['order_id'], 'max_rows': 10}),
    ('document-confluence', {'server': 'https://docs.example.test/wiki', 'username': 'reader@example.test'},
     {'page_id': '123'}, {'page_id': '456'}, {'equals': {'page_id': '123'}}),
])
def test_connection_only_db_and_document_recipes_keep_task_read_scope(registry, recipe, inputs, allowed, denied, approved):
    from daeri.setup import build_setup_resource
    resource = build_setup_resource(registry.store, recipe, 'service', 'resource-one', inputs)
    constraints = resource['capabilities'][0]['constraints']
    actual = copy.deepcopy(allowed)
    interpret(resource['adapter'], actual, constraints, approved)
    if recipe == 'db-postgres':
        assert actual['sql'].endswith('LIMIT 10')
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        interpret(resource['adapter'], denied, constraints, approved)
    with pytest.raises(StoreError):
        interpret(resource['adapter'], copy.deepcopy(allowed), constraints, {})


def test_profile_revision_or_schema_change_requires_new_diagnosis(registry):
    p = profile()
    registry.diagnose(p, "source-one")
    invocation = registry.normalize(p, "source-one", "read-file", {"ref": "main", "file_path": "src/main.py"}, scope())
    p["resources"][0]["connection"]["env"]["FIXTURE_MODE"] = "changed"
    with pytest.raises(StoreError, match="CONTEXT_CHANGED"):
        registry.get(p, "source-one", "read-file")
    invocation.connection["env"]["FIXTURE_MODE"] = "changed"
    with pytest.raises(StoreError, match="CONTEXT_CHANGED"):
        registry.execute(invocation)


def test_result_schema_failure_is_not_success(registry):
    p = profile()
    registry.diagnose(p, "source-one")
    invocation = registry.normalize(p, "source-one", "read-file", {"ref": "main", "file_path": "src/invalid.py"}, scope())
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        registry.execute(invocation)


def test_remote_failure_never_exposes_remote_error_body(registry):
    p = profile()
    registry.diagnose(p, "source-one")
    invocation = registry.normalize(p, "source-one", "read-file", {"ref": "main", "file_path": "src/fail.py"}, scope())
    with pytest.raises(StoreError, match="EXTERNAL_EXECUTION_FAILED") as caught:
        registry.execute(invocation)
    assert "should-not-leak" not in str(caught.value)


def test_timeout_is_unknown_without_automatic_retry(registry):
    p = profile()
    registry.diagnose(p, "source-one")
    invocation = registry.normalize(p, "source-one", "read-file", {"ref": "main", "file_path": "src/slow.py"}, scope())
    with pytest.raises(StoreError, match="EXTERNAL_OUTCOME_UNKNOWN"):
        registry.execute(invocation, timeout=0.25)


@pytest.mark.parametrize("mutate", [
    lambda c: c["connection"].update(command="sh"),
    lambda c: c["connection"].update(env={"PASSWORD": "plain"}),
    lambda c: c["capabilities"][0].update(tool_name="delete_file"),
    lambda c: c.update(unexpected=True),
])
def test_unsafe_local_registration_rejected(mutate):
    resource = config()
    mutate(resource)
    with pytest.raises(StoreError):
        validate_resource(resource)


def test_registration_has_no_cross_profile_reuse(registry):
    p = profile()
    registry.diagnose(p, "source-one")
    other = copy.deepcopy(p)
    other["profile_id"] = "profile_other"
    with pytest.raises(StoreError, match="CONNECTION_NOT_READY"):
        registry.get(other, "source-one", "read-file")


def test_select_enforces_actual_table_columns_and_row_limit():
    arguments = {"sql": "SELECT order_id, SUM(amount) FROM public.orders GROUP BY order_id"}
    policy = {"sql_field": "sql", "tables": ["public.orders"], "columns": ["order_id", "amount"], "max_rows": 100}
    access = interpret("sql_read", arguments, policy, {"tables": ["public.orders"], "columns": ["order_id", "amount"], "max_rows": 20})
    assert access == {"tables": ["public.orders"], "columns": ["amount", "order_id"], "max_rows": 20}
    assert arguments["sql"].endswith("LIMIT 20")


@pytest.mark.parametrize('sql,columns', [
    ('SELECT id FROM other_schema.new_table', ['id']),
    ('SELECT * FROM other_schema.new_table', ['*']),
    ('SELECT t.* FROM other_schema.new_table AS t', ['*']),
    ('SELECT COUNT(*) FROM information_schema.tables', []),
])
def test_db_whole_access_reads_any_table_and_records_actual_columns(sql, columns):
    arguments = {'sql': sql}
    constraints = {'tables': ['*'], 'columns': ['*'], 'max_rows': 100}
    access = interpret('postgres_read', arguments, constraints,
                       {'tables': ['*'], 'columns': ['*'], 'max_rows': 20})
    assert access == {'tables': ['information_schema.tables' if 'information_schema' in sql else 'other_schema.new_table'],
                      'columns': columns, 'max_rows': 20}
    assert arguments['sql'].endswith('LIMIT 20')


@pytest.mark.parametrize('whole_at', ['connection', 'plan'])
@pytest.mark.parametrize('sql', [
    'SELECT amount FROM public.orders', 'SELECT order_id FROM private.orders',
    'SELECT * FROM public.orders',
])
def test_db_whole_access_does_not_override_the_other_scope(whole_at, sql):
    whole = {'tables': ['*'], 'columns': ['*'], 'max_rows': 100}
    narrow = {'tables': ['public.orders'], 'columns': ['order_id'], 'max_rows': 10}
    constraints, scope = (whole, narrow) if whole_at == 'connection' else (narrow, whole)
    allowed = {'sql': 'SELECT order_id FROM public.orders'}
    assert interpret('postgres_read', allowed, constraints, scope)['max_rows'] == 10
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        interpret('postgres_read', {'sql': sql}, constraints, scope)


@pytest.mark.parametrize('sql', [
    'DELETE FROM public.orders', 'SELECT * FROM public.orders FOR UPDATE',
    'SELECT pg_sleep(1) FROM public.orders', 'SELECT * FROM public.orders; SELECT * FROM public.users',
])
def test_db_whole_access_keeps_read_only_sql_boundaries(sql):
    whole = {'tables': ['*'], 'columns': ['*'], 'max_rows': 10}
    with pytest.raises(StoreError):
        interpret('postgres_read', {'sql': sql}, whole, whole)


def test_db_whole_table_scope_does_not_grant_columns_to_aggregate_only_access():
    aggregate = {'tables': ['*'], 'columns': [], 'max_rows': 10}
    assert interpret('postgres_read', {'sql': 'SELECT COUNT(*) FROM public.orders'}, aggregate, aggregate) == {
        'tables': ['public.orders'], 'columns': [], 'max_rows': 10}
    with pytest.raises(StoreError, match='SCOPE_VIOLATION'):
        interpret('postgres_read', {'sql': 'SELECT id FROM public.orders'}, aggregate, aggregate)


@pytest.mark.parametrize('field,values', [('tables', ['*', 'public.orders']), ('columns', ['*', 'id'])])
def test_db_whole_access_cannot_be_mixed_with_an_explicit_list(field, values):
    resource = config()
    resource.update(type='DB', adapter='postgres_read', target={'database': 'service'})
    resource['connection']['env'] = {}
    resource['capabilities'] = [{'capability_id': 'query', 'tool_name': 'execute_sql',
                                 'constraints': {'tables': ['*'], 'columns': ['*'], 'max_rows': 10, field: values}}]
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        validate_resource(resource)


def test_sql_exact_query_scope_preserves_approved_filter():
    allowed = "SELECT order_id FROM public.orders WHERE order_id = 1"
    constraints = {"tables": ["public.orders"], "columns": ["order_id"], "max_rows": 10}
    approved = {"equals": {"sql": allowed}, "tables": ["public.orders"], "columns": ["order_id"], "max_rows": 10}
    query = {"sql": allowed}
    interpret("sql_read", query, constraints, approved)
    assert "WHERE order_id = 1" in query["sql"]
    with pytest.raises(StoreError, match="SCOPE_VIOLATION"):
        interpret("sql_read", {"sql": "SELECT order_id FROM public.orders"}, constraints, approved)


@pytest.mark.parametrize("sql", [
    "DELETE FROM public.orders", "SELECT order_id FROM public.orders; DELETE FROM public.orders",
    "SELECT * FROM public.orders", "SELECT password FROM public.orders", "SELECT order_id FROM private.orders",
    "SELECT pg_sleep(2) FROM public.orders", "SELECT nextval('seq') FROM public.orders",
    "WITH x AS (DELETE FROM public.orders RETURNING *) SELECT * FROM x",
    "SELECT order_id INTO public.copy FROM public.orders", "SELECT order_id FROM public.orders FOR UPDATE",
    "SELECT order_id FROM public.orders UNION SELECT secret FROM private.accounts",
    "SELECT order_id FROM public.orders JOIN private.accounts ON true",
])
def test_sql_cannot_escape_read_scope(sql):
    with pytest.raises(StoreError):
        interpret("sql_read", {"sql": sql}, {"tables": ["public.orders"], "columns": ["order_id", "amount"], "max_rows": 100},
                  {"tables": ["public.orders"], "columns": ["order_id", "amount"], "max_rows": 20})


def test_infra_command_is_parsed_and_keeps_explicit_region():
    command = "aws ec2 describe-instances --region ap-northeast-2"
    access = interpret("aws_read", {"cli_command": command}, {"region": "ap-northeast-2"}, {"equals": {"cli_command": command}})
    assert access == {"service": "ec2", "operation": "describe-instances", "region": "ap-northeast-2"}


@pytest.mark.parametrize("command", [
    "aws ec2 terminate-instances --region ap-northeast-2", "aws sts assume-role --region ap-northeast-2",
    "aws ec2 describe-instances --region us-east-1", "aws ec2 describe-instances --region ap-northeast-2; echo nope",
    "aws ec2 describe-instances --region ap-northeast-2 --endpoint-url=https://other.test",
    "aws ec2 describe-instances --region ap-northeast-2 --profile other",
    "aws ec2 describe-instances --region ap-northeast-2 --filters file://private.json",
    "aws ec2 describe-instances --region ap-northeast-2 --filters=file://private.json",
    "aws ec2 describe-instances --region ap-northeast-2 --prof other",
    "aws ec2 describe-instances --region ap-northeast-2 --end https://other.test",
    "aws ec2 describe-instances --region ap-northeast-2 --reg us-east-1",
    "aws ec2 describe-instances --region ap-northeast-2 --region=us-east-1",
    "aws ec2 describe-instances --region ap-northeast-2 --ca-bundle /private/file",
])
def test_infra_command_cannot_change_operation_target_or_local_files(command):
    with pytest.raises(StoreError):
        interpret("aws_read", {"cli_command": command}, {"region": "ap-northeast-2"}, {"equals": {"cli_command": command}})


def test_cache_keys_are_literal_and_document_queries_are_not_inferred():
    with pytest.raises(StoreError):
        interpret("redis_read", {"key": "session:*"}, {}, {"equals": {"key": "session:*"}})
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        interpret("document_read", {"cql": "space=PRIVATE"}, {}, {"equals": {"cql": "space=PRIVATE"}})


def test_schema_external_reference_is_rejected_before_any_call():
    from daeri.resources import _schema
    with pytest.raises(StoreError):
        _schema({"type": "object", "properties": {"value": {"$ref": "https://other.test/schema"}}})
    _schema({"type": "object", "$defs": {"safe": {"type": "string"}}, "properties": {"value": {"$ref": "#/$defs/safe"}}})


def test_credential_rotation_invalidates_old_contract_and_inflight_request(registry):
    secrets = {"auth_one": "credential-first"}
    registry.resolve = secrets.__getitem__
    resource = config()
    resource["connection"]["auth_env"] = {"GITLAB_TOKEN": "auth_one"}
    p = profile(resource)
    registry.diagnose(p, "source-one")
    before = registry.get(p, "source-one", "read-file")["contract_ref"]
    invocation = registry.normalize(p, "source-one", "read-file", {"ref": "main", "file_path": "src/main.py"}, scope())
    secrets["auth_one"] = "credential-second"
    with pytest.raises(StoreError, match="CONTEXT_CHANGED"):
        registry.execute(invocation)
    with pytest.raises(StoreError, match="CONTEXT_CHANGED"):
        registry.normalize(p, "source-one", "read-file", {"ref": "main", "file_path": "src/main.py"}, scope())
    registry.diagnose(p, "source-one")
    assert registry.get(p, "source-one", "read-file")["contract_ref"] != before
    assert "credential-first" not in "".join(path.read_text() for path in registry.directory.glob("*.json"))


def test_source_target_mismatch_is_rejected_without_starting_lower_mcp(registry):
    resource = config()
    resource["connection"]["env"]["GITLAB_URL"] = "https://other.test"
    with pytest.raises(StoreError, match="CONTEXT_CHANGED"):
        registry.diagnose(profile(resource), "source-one")


def test_db_uri_cannot_select_another_database(registry):
    resource = config()
    resource.update(type="DB", adapter="sql_read", target={"host": "db.example.test", "port": 5432, "database": "service"})
    resource["connection"]["env"] = {}
    resource["connection"]["auth_env"] = {"DATABASE_URI": "database-auth"}
    registry.resolve = lambda ref: "postgresql://reader:private@db.example.test:5432/other?sslmode=require"
    with pytest.raises(StoreError, match="CONTEXT_CHANGED"):
        registry._binding(resource)
    registry.resolve = lambda ref: "postgresql://reader:private@db.example.test:5432/service?sslmode=require"
    fingerprint, values = registry._binding(resource)
    assert len(fingerprint) == 64
    assert "private" not in fingerprint


def db_password_config(registry):
    from daeri.setup import build_setup_resource
    return build_setup_resource(registry.store, 'db-postgres', 'service', 'primary-db', {
        'host': 'db.example.test', 'port': 5432, 'database': 'service/db', 'username': 'reader@example'})


def test_new_db_setup_requires_only_password_and_keeps_public_target(registry):
    resource = db_password_config(registry)
    assert resource['connection']['auth_env'] == {
        'DATABASE_PASSWORD': 'credential:service.primary-db.database_password'}
    assert resource['target'] == {
        'host': 'db.example.test', 'port': 5432, 'database': 'service/db', 'username': 'reader@example'}


@pytest.mark.parametrize('host,authority', [('db.example.test', 'db.example.test'),
                                          ('2001:db8::1', '[2001:db8::1]'),
                                          ('[2001:db8::1]', '[2001:db8::1]')])
def test_db_password_builds_only_selected_endpoint_in_private_environment(registry, host, authority):
    from urllib.parse import quote, unquote, urlsplit
    resource = db_password_config(registry)
    resource['target']['host'] = host
    original = copy.deepcopy(resource)
    password = 'private:/@?#%+ value'
    ref = next(iter(resource['connection']['auth_env'].values()))
    registry.resolve = lambda selected: password
    fingerprint, credentials = registry._binding(resource)
    environment = get_adapter('postgres_read').session_environment(resource, {'DATABASE_PASSWORD': credentials[ref]})
    assert environment == {
        'DATABASE_URI': f'postgresql://reader%40example:{quote(password, safe="")}@{authority}:5432/service%2Fdb?sslmode=require'}
    parsed = urlsplit(environment['DATABASE_URI'])
    assert unquote(parsed.password) == password
    assert unquote(parsed.username) == resource['target']['username']
    assert unquote(parsed.path[1:]) == resource['target']['database']
    assert parsed.port == 5432
    assert credentials == {ref: password}
    assert resource == original
    assert password not in json.dumps(registry._launch_connection(resource))
    assert environment['DATABASE_URI'] not in json.dumps(registry._launch_connection(resource))
    registry.resolve = lambda selected: password + 'rotated'
    assert registry._binding(resource)[0] != fingerprint


@pytest.mark.parametrize('target', [
    {'host': 'primary.example,other.example'}, {'host': '/tmp/postgres'}, {'host': 'primary.example@other.example'},
    {'host': 'primary.example?host=other.example'}, {'host': 'primary.example:5432'}, {'host': 'primary%2eexample'},
    {'host': '[invalid]'}, {'port': 5432.0}, {'port': True}, {'port': 0}, {'port': 65536},
    {'database': ''}, {'username': ''}, {'database': 'service\noptions=-c'},
])
def test_db_password_rejects_ambiguous_targets_before_launch(registry, target):
    resource = db_password_config(registry)
    resource['target'].update(target)
    registry.resolve = lambda ref: 'private'
    with pytest.raises(StoreError):
        registry._binding(resource)


def test_db_password_and_legacy_uri_require_one_explicit_authentication_mode(registry):
    resource = db_password_config(registry)
    resource['connection']['auth_env']['DATABASE_URI'] = 'credential:legacy.uri'
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        validate_resource(resource)


@pytest.mark.parametrize('password', ['private://[invalid]:/@?#%+ value', '1', 'A'])
def test_db_runtime_secret_environment_masks_password_and_derived_uri(registry, monkeypatch, password):
    import asyncio
    from contextlib import AsyncExitStack, asynccontextmanager
    from urllib.parse import quote
    resource = db_password_config(registry)
    ref = next(iter(resource['connection']['auth_env'].values()))
    registry.resolve = lambda selected: password
    _, credentials = registry._binding(resource)
    captured = {}

    @asynccontextmanager
    async def transport(parameters, **kwargs):
        captured.update(environment=parameters.env, cwd=str(parameters.cwd), arguments=parameters.args)
        yield None, None

    class Session:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): pass
        async def send_request(self, *args):
            from mcp.types import CallToolResult
            body = '|'.join([password, quote(password, safe=''), captured['environment']['DATABASE_URI']])
            return CallToolResult(content=[], structuredContent={'body': body, 'count': 1})

    monkeypatch.setattr('daeri.resources.stdio_client', transport)
    monkeypatch.setattr('daeri.resources.ClientSession', Session)
    async def run():
        async with AsyncExitStack() as stack:
            _, secrets = await registry._session(stack, resource, 30, credentials)
            return secrets
    secrets = asyncio.run(run())
    assert 'DATABASE_PASSWORD' not in captured['environment']
    assert captured['environment']['DATABASE_URI'] in secrets
    assert password in secrets
    assert quote(password, safe='') in secrets
    assert captured['environment']['DATABASE_URI'] not in captured['cwd'] + repr(captured['arguments'])
    if len(password) > 1:
        assert password not in captured['cwd'] + repr(captured['arguments'])

    async def registered_tools(session):
        return [{'name': 'execute_sql', 'inputSchema': {
            'type': 'object', 'properties': {'sql': {'type': 'string'}}, 'required': ['sql'],
            'additionalProperties': False}}]
    monkeypatch.setattr(registry, '_tools', registered_tools)
    original = copy.deepcopy(resource)
    current = profile(resource)
    registry.diagnose(current, 'primary-db')
    invocation = registry.normalize(current, 'primary-db', 'query', {'sql': 'SELECT * FROM public.orders'},
                                    {'tables': ['*'], 'columns': ['*'], 'max_rows': 10})
    result = registry.execute(invocation)
    assert result['result']['data']['body'] == '[REDACTED]|[REDACTED]|[REDACTED]'
    assert result['result']['data']['count'] == 1
    assert resource == original
    if len(password) > 1:
        assert password not in repr(invocation)
        assert password.encode() not in b''.join(path.read_bytes() for path in registry.directory.rglob('*') if path.is_file())


@pytest.mark.parametrize('mode', ['require', 'verify-ca', 'verify-full'])
def test_db_password_uses_explicit_allowed_tls_mode(registry, mode):
    resource = db_password_config(registry)
    resource['connection']['env']['PGSSLMODE'] = mode
    registry.resolve = lambda ref: 'private'
    registry._binding(resource)
    environment = get_adapter('postgres_read').session_environment(resource, {'DATABASE_PASSWORD': 'private', 'PGSSLMODE': mode})
    assert environment['DATABASE_URI'].endswith('?sslmode=' + mode)


def test_db_password_rejects_disabled_tls_and_legacy_uri_settings_remain_explicit(registry):
    from daeri.setup import build_setup_resource
    resource = db_password_config(registry)
    resource['connection']['env']['PGSSLMODE'] = 'disable'
    registry.resolve = lambda ref: 'private'
    with pytest.raises(StoreError, match='UNSUPPORTED_CONSTRAINT'):
        registry._binding(resource)
    resource['connection']['env'] = {}
    resource['connection']['auth_env'] = {'DATABASE_URI': 'credential:service.primary-db.database_uri'}
    updated = build_setup_resource(registry.store, 'db-postgres', 'service', 'primary-db', {'port': 6432}, existing=resource)
    assert updated['connection']['auth_env'] == resource['connection']['auth_env']
    legacy_environment = {'DATABASE_URI': 'postgresql://reader:private@db.example.test/service'}
    assert get_adapter('postgres_read').session_environment(resource, legacy_environment) == legacy_environment


def test_infra_sso_binding_checks_account_and_provider_changes(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    aws = tmp_path / ".aws"
    aws.mkdir()
    settings = aws / "config"
    settings.write_text("[profile service]\nsso_account_id=111122223333\nregion=ap-northeast-2\n")
    resource = config()
    resource.update(type="Infra", adapter="aws_read", target={"account": "111122223333", "profile": "service", "region": "ap-northeast-2"})
    resource["connection"]["env"] = {"AWS_PROFILE": "service", "AWS_REGION": "ap-northeast-2"}
    before, _ = registry._binding(resource)
    (aws / "credentials").write_text("[service]\naws_access_key_id=example\n")
    after, _ = registry._binding(resource)
    assert before != after
    settings.write_text("[profile service]\nsso_account_id=999900001111\n")
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        registry._binding(resource)


def test_legacy_json_and_sql_literals_preserve_actual_types():
    from mcp.types import CallToolResult, TextContent
    def result(text):
        return CallToolResult(content=[TextContent(type="text", text=text)])
    assert get_adapter('source_read').legacy_result(result('[{"id":9007199254740993,"amount":"10.25"}]')) == {"data": [{"id": 9007199254740993, "amount": "10.25"}]}
    assert get_adapter('sql_read').legacy_result(result("[{'id': 1, 'active': True, 'memo': None}]")) == {"data": [{"id": 1, "active": True, "memo": None}]}
    expression = "[{'created': datetime.date(2026, 1, 1)}]"
    assert get_adapter('sql_read').legacy_result(result(expression)) == {"data": expression}
    assert get_adapter('source_read').legacy_result(result("[{'id':1}]")) == {"data": "[{'id':1}]"}


@pytest.mark.parametrize("text", ['{"key":1,"key":2}', '[NaN]', '[Infinity]'])
def test_legacy_json_ambiguity_is_not_usable_data(text):
    from mcp.types import CallToolResult, TextContent
    with pytest.raises(StoreError, match="RESULT_VALIDATION_FAILED"):
        get_adapter('source_read').legacy_result(CallToolResult(content=[TextContent(type='text', text=text)]))


def test_lower_provider_error_conventions_are_failures():
    from mcp.types import CallToolResult, TextContent
    with pytest.raises(StoreError, match="EXTERNAL_EXECUTION_FAILED"):
        get_adapter('sql_read').legacy_result(CallToolResult(content=[TextContent(type='text', text='Error: permission denied')]))
    with pytest.raises(StoreError, match="EXTERNAL_EXECUTION_FAILED"):
        get_adapter('aws_read').provider_error({'result': [{'cli_command': 'aws ec2 describe-instances', 'error': 'not allowed'}]})


def test_sdk_generated_content_wrapper_is_normalized_to_rows():
    from mcp.types import CallToolResult, TextContent
    text = "[{'order_id':1,'amount':'10.25'}]"
    wire = CallToolResult(content=[TextContent(type="text", text=text)],
                          structuredContent={"result": [{"type": "text", "text": text}]})
    assert get_adapter('sql_read').legacy_result(wire) == {"data": [{"order_id": 1, "amount": "10.25"}]}


def test_decimal_repr_is_exact_and_marked_by_the_adapter():
    from mcp.types import CallToolResult, TextContent
    types = {}
    result = get_adapter('sql_read').legacy_result(CallToolResult(content=[TextContent(type='text', text="[{'amount': Decimal('12345678901234567890.125'), 'memo': '10.25'}]")]), value_types=types)
    assert result == {"data": [{"amount": "12345678901234567890.125", "memo": "10.25"}]}
    assert types == {"/data/0/amount": "decimal"}


def test_prepared_redis_launcher_uses_registered_target_instead_of_cli_defaults(registry):
    resource = config()
    resource["adapter"] = "redis_read"
    resource["connection"]["command"] = "/prepared/redis-mcp-server"
    resource["connection"]["args"] = []
    resource["connection"]["env"] = {"REDIS_HOST": "cache.example.test", "REDIS_PORT": "6380", "REDIS_DB": "2", "REDIS_SSL": "true"}
    connection = registry._launch_connection(resource)
    assert connection["args"] == ["--host", "cache.example.test", "--port", "6380", "--db", "2", "--ssl"]


def aws_config(tmp_path, monkeypatch, extra=""):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "config").write_text("[profile service]\nsso_account_id=111122223333\nregion=ap-northeast-2\n" + extra)
    resource = config()
    resource.update(type="Infra", adapter="aws_read", target={"account": "111122223333", "profile": "service", "region": "ap-northeast-2"})
    resource["connection"]["env"] = {"AWS_PROFILE": "service", "AWS_REGION": "ap-northeast-2"}
    return resource, aws


@pytest.mark.parametrize("setting", [
    "endpoint_url=https://other.test\n", "services=redirect\n", "credential_process=/unapproved/helper\n",
    "web_identity_token_file=/unapproved/token\n", "credential_source=Ec2InstanceMetadata\n", "region=us-east-1\n",
])
def test_aws_provider_overrides_cannot_bypass_target_or_execute_programs(registry, tmp_path, monkeypatch, setting):
    resource, _ = aws_config(tmp_path, monkeypatch, setting)
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        registry._binding(resource)


@pytest.mark.parametrize("field,value", [
    ("AWS_ENDPOINT_URL", "https://other.test"), ("AWS_ENDPOINT_URL_EC2", "https://other.test"),
    ("AWS_CONFIG_FILE", "/other/config"), ("AWS_API_MCP_PROFILE_NAME", "other"), ("AWS_DEFAULT_REGION", "us-east-1"),
    ("AWS_DATA_PATH", "/unapproved/models"), ("EXPERIMENTAL_AGENT_SCRIPTS", "true"),
    ("aws_config_file", "/other/config"), ("aws_profile", "other"),
])
def test_aws_environment_overrides_are_rejected_before_launch(registry, tmp_path, monkeypatch, field, value):
    resource, _ = aws_config(tmp_path, monkeypatch)
    resource["connection"]["env"][field] = value
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        registry._binding(resource)


def test_aws_source_profile_process_and_global_plugins_are_blocked(registry, tmp_path, monkeypatch):
    resource, aws = aws_config(tmp_path, monkeypatch)
    (aws / "config").write_text("[profile service]\nrole_arn=arn:aws:iam::111122223333:role/reader\nsource_profile=base\nregion=ap-northeast-2\n[profile base]\ncredential_process=/unapproved/helper\n")
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        registry._binding(resource)
    (aws / "config").write_text("[profile service]\nsso_account_id=111122223333\nregion=ap-northeast-2\n[plugins]\ncustom=/unapproved/plugin\n")
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        registry._binding(resource)


def test_aws_launch_pins_provider_profile_and_defense_in_depth(registry):
    resource = config()
    resource["adapter"] = "aws_read"
    resource["connection"]["env"] = {"AWS_PROFILE": "service", "AWS_REGION": "ap-northeast-2"}
    env = registry._launch_connection(resource)["env"]
    assert env["AWS_API_MCP_PROFILE_NAME"] == "service"
    assert env["AWS_DEFAULT_REGION"] == "ap-northeast-2"
    assert env["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] == "true"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["READ_OPERATIONS_ONLY"] == "true"
    assert env["AWS_API_MCP_ALLOW_UNRESTRICTED_LOCAL_FILE_ACCESS"] == "no-access"


def test_standard_provider_login_configuration_remains_usable_and_cache_bound(registry, tmp_path, monkeypatch):
    from daeri.provider_login import _configuration
    import io

    resource, aws = aws_config(tmp_path, monkeypatch)
    request = {"profile": "service", "account": "111122223333", "region": "ap-northeast-2",
               "start_url": "https://example.awsapps.com/start", "sso_region": "ap-northeast-2", "role": "Reader"}
    # Exercise the real login writer's new-profile SSO-session configuration.
    (aws / "config").unlink()
    parser, *_ = _configuration(request, tmp_path)
    content = io.StringIO()
    parser.write(content)
    (aws / "config").write_text(content.getvalue())
    cache = aws / "sso" / "cache"
    cache.mkdir(parents=True)
    token = cache / "local-session.json"
    token.write_text('{"accessToken":"PRIVATE_FIRST_SENTINEL"}')
    first, credentials = registry._binding(resource)
    assert credentials == {}
    assert "PRIVATE_FIRST_SENTINEL" not in first
    token.write_text('{"accessToken":"PRIVATE_SECOND_SENTINEL"}')
    assert registry._binding(resource)[0] != first


@pytest.mark.parametrize("variable", ["PYTHONPATH", "DYLD_INSERT_LIBRARIES", "HTTPS_PROXY"])
def test_resource_environment_cannot_inject_programs_or_proxy_credentials(variable):
    resource = config()
    resource["connection"]["env"][variable] = "/unapproved"
    with pytest.raises(StoreError):
        validate_resource(resource)


def test_prepared_source_cli_cannot_override_registered_connection():
    resource = config()
    resource["connection"]["command"] = "/prepared/mcp-gitlab"
    resource["connection"]["args"] = ["--url", "https://other.test"]
    with pytest.raises(StoreError):
        validate_resource(resource)


def test_auth_references_cannot_replace_a_registered_endpoint():
    resource = config()
    resource["connection"]["auth_env"] = {"GITLAB_URL": "auth_other_endpoint"}
    with pytest.raises(StoreError, match="UNSUPPORTED_CONSTRAINT"):
        validate_resource(resource)


def test_remote_auth_headers_cannot_change_http_authority():
    resource = config()
    resource["connection"] = {"transport": "streamable_http", "url": "https://mcp.example.test/mcp", "auth_headers": {"Host": "auth_other_authority"}}
    with pytest.raises(StoreError):
        validate_resource(resource)


def test_canonical_product_adapter_preserves_registered_configuration():
    resource = config()
    resource["adapter"] = "gitlab_read"
    original = copy.deepcopy(resource)
    assert validate_resource(resource) == original
    assert resource == original


def test_legacy_adapter_cannot_be_registered_as_another_resource_type():
    resource = config()
    resource["type"] = "DB"
    with pytest.raises(StoreError, match="INVALID_INPUT"):
        validate_resource(resource)
