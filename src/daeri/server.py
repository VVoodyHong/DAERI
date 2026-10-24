"""MCP boundary for shared task execution, worker delegation and profile memory."""

import asyncio
from importlib.metadata import version
import json
import logging
import sys

import anyio

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp import types

from .cli import data_directory
from .contracts import INPUT_MODELS
from .engine import Engine


DESCRIPTIONS = {
    "daeri_task_create": "Store a local task request with the supplied profile context. Unresolved context enters awaiting_input before planning.",
    "daeri_task_update": "Clarify or pause a local task with its expected revision. Pausing invalidates existing approval.",
    "daeri_task_get": "Read stored local task state or recent tasks for the selected profile.",
    "daeri_task_resume": "Check task ownership and prepare local resumption. A paused task requires fresh plan approval.",
    "daeri_task_cancel": "Cancel a local task and block further dispatch while preserving recorded prior effects.",
    "daeri_memory_search": "Search permitted profile knowledge before planning. An explicitly selected company embedding API may receive the permitted query; storage and indexes remain local. Reports keyword-only or incomplete retrieval explicitly.",
    "daeri_resource_list": "Read locally registered resource and output-format summaries for the fixed profile, including current availability and contract references.",
    "daeri_plan_submit": "Validate and store an immutable plan against registered read capabilities, recorded memory lookup, field policies, dependencies and cumulative budget. Omitted memory and source-retention rules are resolved into the displayed plan for automatic learning. Requires user approval before execution.",
    "daeri_plan_get": "Read a stored plan and its current approval state under present access and retention conditions.",
    "daeri_approval_request": "Request approval in the current host conversation, then link the user's actual reply and interpreted decision to the current immutable plan and pending request.",
    "daeri_capability_get": "Read the registered local capability contract for a task's fixed profile, including supported purposes, schemas and constraints.",
    "daeri_capability_execute": "Execute a registered read capability within the current approved plan, field policy and remaining cumulative budget.",
    "daeri_result_submit": "Validate evidence, completion checks, derived tables and approved file formats, then store the report and automatically remember verified experience. Supply memory_candidates for reusable rules, structure or feedback; otherwise completed verified calls produce a procedure memory. Indexing may send permitted knowledge to the selected company embedding API. Returns report and memory status together.",
    "daeri_result_get": "Read a locally registered record or managed artifact after current permission, masking, integrity and expiry checks.",
    "daeri_memory_update": "Store or invalidate selected, evidenced profile knowledge within the approved memory policy and source-field provenance. Indexing may send permitted knowledge to an explicitly selected company embedding API.",
    "daeri_worker": "Issue or revoke scoped delegations under the current approved task; the host creates the actual workers. Workers use their scoped token to inspect assignments, search shared profile memory, execute assigned steps, read permitted evidence and submit findings. The root agent integrates evidence and submits the final report with memory candidates; the runtime validates and publishes eligible memory. All calls share the root task budget.",
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": ["ok", "pending", "partial", "error"]},
        "data": {"type": ["object", "null"]},
        "error": {
            "type": ["object", "null"],
            "properties": {"code": {"type": "string"}, "message": {"type": "string"}, "next_action": {"type": "string"}},
            "required": ["code", "message", "next_action"],
            "additionalProperties": False,
        },
    },
    "required": ["status", "data", "error"],
    "additionalProperties": False,
}


def tool_result(result):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, allow_nan=False))],
        structuredContent=result,
        isError=result["status"] in {"error", "partial"},
    )


def create_server(engine):
    server = Server(
        "operations", version=version("daeri-agent"),
        instructions="Use the fixed DAERI profile. The host root agent owns planning, user approval, work assignment and evidence integration. Search memory and inspect registered contracts before planning. Obtain the user's actual reply in the current conversation before resource execution. Delegate independent approved steps through daeri_worker and the host's native workers when useful. Root and workers choose calls within their approved steps, method, scope, dependencies, field policy and shared cumulative budget. The root submits the final report with curated memory candidates; the runtime automatically validates, stores and indexes eligible knowledge. Environment preparation and authentication use the explicit setup management path.",
    )

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=name,
                description=DESCRIPTIONS[name],
                inputSchema=model.model_json_schema(),
                outputSchema=OUTPUT_SCHEMA,
                annotations=types.ToolAnnotations(
                    readOnlyHint=name in {"daeri_task_get", "daeri_plan_get", "daeri_resource_list", "daeri_capability_get", "daeri_result_get"},
                    openWorldHint=name in {"daeri_capability_execute", "daeri_memory_search", "daeri_memory_update", "daeri_result_submit", "daeri_worker"},
                ),
            )
            for name, model in INPUT_MODELS.items()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            return tool_result(await anyio.to_thread.run_sync(engine.call, name, arguments))
        except Exception:
            print("DAERI local request failed (STORE_UNAVAILABLE).", file=sys.stderr)
            return tool_result({
                "status": "error", "data": None,
                "error": {"code": "STORE_UNAVAILABLE", "message": "Local request could not be completed.", "next_action": "stop"},
            })

    registered_call = server.request_handlers[types.CallToolRequest]

    async def checked_call(request):
        # The SDK's v1 decorator catches McpError, so reject unknown names outside it.
        if request.params.name not in INPUT_MODELS:
            raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message="Unknown tool"))
        return await registered_call(request)

    server.request_handlers[types.CallToolRequest] = checked_call
    return server


async def serve():
    engine = Engine(data_directory())
    try:
        server = create_server(engine)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        engine.close()


def main():
    # SDK root-logger validation messages include payloads; emit only our sanitized diagnostics.
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("DAERI could not start (STORE_UNAVAILABLE). Check the local environment and data directory.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
