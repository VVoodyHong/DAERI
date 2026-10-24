import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from jsonschema import Draft202012Validator
import pytest

from daeri.storage import Store


ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = {
    "daeri_task_create", "daeri_task_update", "daeri_task_get", "daeri_task_resume",
    "daeri_task_cancel", "daeri_memory_search", "daeri_resource_list",
    "daeri_plan_submit", "daeri_plan_get", "daeri_approval_request",
    "daeri_capability_get", "daeri_capability_execute", "daeri_result_submit", "daeri_result_get", "daeri_memory_update", "daeri_worker",
}


@asynccontextmanager
async def connect(data_dir, *, launch=False, plugin_root=ROOT):
    parameters = StdioServerParameters(
        command="/bin/sh" if launch else sys.executable,
        args=[str(plugin_root / "scripts/launch.sh")] if launch else ["-I", "-m", "daeri.server"],
        env={"DAERI_DATA_DIR": str(data_dir), "__PYVENV_LAUNCHER__": ""},
        cwd=data_dir.parent,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=10)) as session:
            initialized = await session.initialize()
            yield session, initialized


def envelope(result):
    data = result.structuredContent
    assert set(data) == {"status", "data", "error"}
    assert json.loads(result.content[0].text) == data
    assert result.isError == (data["status"] in {"error", "partial"})
    return data


def test_stdio_handshake_lists_the_implemented_tools_and_strict_schemas(tmp_path):
    async def scenario():
        async with connect(tmp_path / "data") as (session, initialized):
            assert initialized.serverInfo.name == "operations"
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert set(tools) == TOOL_NAMES
            schema = tools["daeri_task_create"].inputSchema
            validator = Draft202012Validator(schema)
            assert validator.is_valid({"request_id": "schema-check", "request_text": "요청"})
            assert not validator.is_valid({"request_id": "schema-check", "request_text": 7})
            assert not validator.is_valid({"request_id": "schema-check", "request_text": "요청", "extra": True})
            assert not validator.is_valid({"request_text": "요청"})
            assert all(tool.description for tool in tools.values())
            for name in {"daeri_capability_execute", "daeri_memory_search", "daeri_memory_update", "daeri_worker"}:
                assert tools[name].annotations.openWorldHint is True
            worker_schema = Draft202012Validator(tools["daeri_worker"].inputSchema)
            assert worker_schema.is_valid({"operation": "get", "task_id": "task", "delegation_token": "token"})
            assert not worker_schema.is_valid({"operation": "get", "task_id": "task", "delegation_token": "token", "expected_task_revision": 1})
            for name in {"daeri_memory_search", "daeri_memory_update"}:
                assert "embedding API" in tools[name].description
            for name in {"daeri_task_update", "daeri_task_cancel", "daeri_task_resume", "daeri_plan_submit", "daeri_approval_request"}:
                assert tools[name].annotations.readOnlyHint is False
                assert tools[name].annotations.destructiveHint is not False
            for tool in tools.values():
                assert tool.outputSchema is not None
                output = Draft202012Validator(tool.outputSchema)
                assert output.is_valid({"status": "pending", "data": {}, "error": None})
                assert not output.is_valid({"status": "ok", "data": {}})
                assert not output.is_valid({"status": "unknown", "data": None, "error": None})

    asyncio.run(scenario())


@pytest.mark.parametrize("launch", [False, pytest.param(True, marks=pytest.mark.skipif(os.name == "nt", reason="POSIX launcher"))])
def test_stdio_create_and_get_use_the_selected_local_data_directory(tmp_path, plugin_environment, launch):
    async def scenario():
        async with connect(tmp_path / "data", launch=launch, plugin_root=plugin_environment) as (session, _):
            created = envelope(await session.call_tool("daeri_task_create", {
                "request_id": "stdio-create", "request_text": "로컬 계획을 준비해줘",
            }))
            assert created["status"] == "ok"
            task = created["data"]
            assert task["state"] == "awaiting_input"
            fetched = envelope(await session.call_tool("daeri_task_get", {"task_id": task["task_id"]}))
            assert fetched["data"]["task"]["task_id"] == task["task_id"]

    asyncio.run(scenario())
    assert (tmp_path / "data" / "daeri.sqlite3").is_file()


def test_invalid_known_tool_input_returns_a_daeri_error_without_echoing_input(tmp_path):
    async def scenario():
        async with connect(tmp_path / "data") as (session, _):
            result = envelope(await session.call_tool("daeri_task_create", {
                "request_id": "invalid-create", "request_text": 7,
                "unknown": "PRIVATE_INVALID_INPUT_SENTINEL",
            }))
            assert result["status"] == "error"
            assert result["error"]["code"] == "INVALID_INPUT"
            assert "PRIVATE_INVALID_INPUT_SENTINEL" not in json.dumps(result)

    asyncio.run(scenario())


def test_unknown_tool_is_a_protocol_error(tmp_path):
    async def scenario():
        async with connect(tmp_path / "data") as (session, _):
            with pytest.raises(McpError) as error:
                await session.call_tool("not_a_daeri_tool", {})
            assert error.value.error.code == -32602

    asyncio.run(scenario())


def test_startup_failure_keeps_stdout_clean_and_does_not_print_private_paths(tmp_path):
    invalid = tmp_path / "PRIVATE_DATA_PATH_SENTINEL"
    invalid.write_text("not a directory")
    result = subprocess.run(
        [sys.executable, "-m", "daeri.server"],
        env=dict(os.environ, DAERI_DATA_DIR=str(invalid)),
        input="", text=True, capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "STORE_UNAVAILABLE" in result.stderr
    assert "PRIVATE_DATA_PATH_SENTINEL" not in result.stderr
    assert "Traceback" not in result.stderr


def test_malformed_protocol_arguments_do_not_leak_input_to_diagnostics(tmp_path):
    message = {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "daeri_task_create", "arguments": ["PRIVATE_PROTOCOL_SENTINEL"]},
    }
    result = subprocess.run(
        [sys.executable, "-m", "daeri.server"],
        env=dict(os.environ, DAERI_DATA_DIR=str(tmp_path / "data")),
        input=json.dumps(message) + "\n", text=True, capture_output=True, timeout=10,
    )
    assert "PRIVATE_PROTOCOL_SENTINEL" not in result.stdout + result.stderr
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert frames[0]["id"] == 7
    assert frames[0]["error"]["code"] == -32602


def test_subprocess_local_plan_approval_pause_and_resume_lifecycle(tmp_path, plugin_environment):
    data_dir = tmp_path / "data"
    store = Store(data_dir)
    try:
        profile = store.put_profile("local-review", core_context=["외부 조회 없는 검토"])
    finally:
        store.close()

    async def scenario():
        async with connect(data_dir, launch=os.name != "nt", plugin_root=plugin_environment) as (session, _):
            async def call(name, args):
                return envelope(await session.call_tool(name, args))

            created = await call("daeri_task_create", {
                "request_id": "lifecycle-create", "request_text": "현재 요청의 로컬 검토 계획",
                "context": {"profile_id": profile["profile_id"]},
            })
            task = created["data"]
            assert created["status"] == "ok"
            search = await call("daeri_memory_search", {"task_id": task["task_id"], "query": "로컬 검토"})
            assert search["status"] == "partial"
            assert search["error"]["code"] == "MEMORY_SEARCH_INCOMPLETE"
            assert search["data"]["retrieval_state"] == "keyword_only"
            assert search["data"]["task_revision"] == task["task_revision"]

            submitted = await call("daeri_plan_submit", {
                "request_id": "lifecycle-plan", "task_id": task["task_id"],
                "expected_task_revision": task["task_revision"],
                "draft": {
                    "purpose": "외부 조회 없이 현재 요청 검토", "steps": [],
                    "budget": {"max_tool_calls": 0, "max_external_requests": 0, "max_runtime_ms": 0, "resource_limits": []},
                    "output": {"format": "text", "destination": "현재 대화", "resource_rules": [], "table_rules": [], "artifact_rules": []},
                    "memory_search_refs": [search["data"]["memory_search_ref"]], "memory_refs": [],
                    "memory_limitations": search["data"]["limitations"],
                    "completion_criteria": [{"criterion_id": "local-review", "description": "현재 요청 검토", "required": True, "step_ids": []}],
                },
            })
            assert submitted["status"] == "ok"
            plan = submitted["data"]
            waiting = await call("daeri_approval_request", {
                "request_id": "lifecycle-request", "task_id": task["task_id"],
                "expected_task_revision": plan["task_revision"],
                "plan_id": plan["plan_id"], "plan_version": plan["plan_version"], "stage": "request",
            })
            assert waiting["status"] == "pending"
            approved = await call("daeri_approval_request", {
                "request_id": "lifecycle-response", "task_id": task["task_id"],
                "expected_task_revision": waiting["data"]["task_revision"],
                "plan_id": plan["plan_id"], "plan_version": plan["plan_version"], "stage": "respond",
                "approval_request_id": waiting["data"]["approval_request_id"],
                "decision": "approve", "user_response": "이 가상 시험 계획을 승인합니다",
            })
            assert approved["status"] == "ok"
            assert approved["data"]["approval_id"]
            paused = await call("daeri_task_update", {
                "request_id": "lifecycle-pause", "task_id": task["task_id"],
                "expected_task_revision": approved["data"]["task_revision"], "action": "pause",
            })
            assert paused["data"]["state"] == "paused"
            resumed = await call("daeri_task_resume", {
                "request_id": "lifecycle-resume", "task_id": task["task_id"],
                "expected_task_revision": paused["data"]["task_revision"],
            })
            assert resumed["status"] == "pending"
            assert resumed["data"]["state"] == "awaiting_approval"
            assert resumed["data"]["resumed"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("use_worker", [False, True])
def test_host_stdio_approval_executes_lower_stdio_and_returns_verified_file(tmp_path, use_worker):
    from daeri.resources import ResourceRegistry
    from test_execution_flow import resource

    data_dir = tmp_path / "data"
    store = Store(data_dir)
    profile = store.put_profile("wire-flow", resources=[resource()])
    ResourceRegistry(store).diagnose(profile, "cache-a")
    store.close()

    async def scenario():
        async with connect(data_dir) as (session, _):
            async def call(name, args):
                return envelope(await session.call_tool(name, args))

            task = (await call("daeri_task_create", {"request_id": "wire-create", "request_text": "Read the approved test key",
                "context": {"profile_id": profile["profile_id"]}}))["data"]
            search = (await call("daeri_memory_search", {"task_id": task["task_id"], "query": "test key"}))["data"]
            descriptor = (await call("daeri_capability_get", {"task_id": task["task_id"], "instance_id": "cache-a", "capability_id": "get"}))["data"]["descriptor"]
            draft = {"purpose": "Read a fixed key", "steps": [{"step_id": "read", "description": "Read the selected test key",
                "purpose": "collect", "depends_on": [], "instance_id": "cache-a", "capability_id": "get",
                "contract_ref": descriptor["contract_ref"], "scope": {"equals": {"key": "orders"}}, "input_bindings": []}],
                "budget": {"max_tool_calls": 1, "max_external_requests": None, "max_runtime_ms": 10000, "resource_limits": []},
                "output": {"format": "json", "destination": "current conversation", "resource_rules": [{"instance_id": "cache-a", **profile["resources"][0]["policy"]}],
                    "table_rules": [], "artifact_rules": [{"artifact_id": "data", "format_id": "json", "format_contract_ref": "format_json_v1", "input_kind": "json", "options": {}, "max_bytes": 10000}]},
                "memory_search_refs": [search["memory_search_ref"]], "memory_refs": [], "memory_limitations": search["limitations"],
                "completion_criteria": [{"criterion_id": "read", "description": "Read the selected key", "required": True, "step_ids": ["read"]}]}
            saved = (await call("daeri_plan_submit", {"request_id": "wire-plan", "task_id": task["task_id"],
                "expected_task_revision": task["task_revision"], "draft": draft}))["data"]
            common = {"task_id": task["task_id"], "plan_id": saved["plan_id"], "plan_version": saved["plan_version"]}
            waiting = (await call("daeri_approval_request", {**common, "request_id": "wire-ask", "stage": "request", "expected_task_revision": saved["task_revision"]}))["data"]
            approved = (await call("daeri_approval_request", {**common, "request_id": "wire-approve", "stage": "respond",
                "expected_task_revision": waiting["task_revision"], "approval_request_id": waiting["approval_request_id"],
                "decision": "approve", "user_response": "이 시험 계획을 승인합니다"}))["data"]
            if use_worker:
                grant = (await call("daeri_worker", {"operation": "create", "request_id": "wire-worker",
                    "task_id": task["task_id"], "expected_task_revision": approved["task_revision"],
                    "assignment": {"label": "조회", "objective": "시험 키 조회", "step_ids": ["read"],
                        "completion_criteria": ["조회 근거 반환"], "source_refs": []}}))["data"]
                async with connect(data_dir) as (worker, _):
                    worker_args = {"task_id": task["task_id"], "delegation_token": grant["delegation_token"]}
                    executed = envelope(await worker.call_tool("daeri_worker", {**worker_args, "operation": "execute",
                        "call": {"step_id": "read", "call_id": "wire-read", "parameters": {"key": "orders"}}}))
                    completed = envelope(await worker.call_tool("daeri_worker", {**worker_args, "operation": "complete",
                        "request_id": "wire-complete", "result": {"summary": "조회 완료",
                            "evidence_refs": [executed["data"]["result_ref"]], "limitations": []}}))
                    assert completed["status"] == "ok", completed
                    revision = completed["data"]["task_revision"]
            else:
                executed = await call("daeri_capability_execute", {**common, "expected_task_revision": approved["task_revision"],
                    "step_id": "read", "call_id": "wire-read", "approval_id": approved["approval_id"], "instance_id": "cache-a", "capability_id": "get",
                    "contract_ref": descriptor["contract_ref"], "parameters": {"key": "orders"}})
                revision = executed["data"]["task_revision"]
            assert executed["status"] == "ok", executed
            ref = executed["data"]["result_ref"]
            report = {"summary": "Read the selected test key", "proposed_task_state": "succeeded",
                "findings": [{"kind": "observation", "text": "The selected key was returned", "evidence_refs": [ref]}], "tables": [],
                "checks": [{"criterion_id": "read", "status": "met", "method": "Verified completed MCP key read", "evidence_refs": [ref]}],
                "limitations": [], "uncertain_effects": [], "next_actions": [],
                "artifacts": [{"kind": "generated", "artifact_id": "data", "source_ref": ref, "label": "Verified key JSON"}]}
            result = await call("daeri_result_submit", {**common, "request_id": "wire-report",
                "expected_task_revision": revision, "report": report})
            assert result["status"] == "ok", result
            assert result["data"]["task_state"] == "succeeded"
            artifact = await call("daeri_result_get", {"task_id": task["task_id"], "record_ref": result["data"]["artifact_refs"][0]})
            assert artifact["status"] == "ok", artifact
            content = json.loads(Path(artifact["data"]["managed_file"]["local_path"]).read_text())
            assert content == {"data": {"rows": [{"id": "one", "amount": "10.00"}]}}

    asyncio.run(scenario())
