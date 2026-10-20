"""Local read MCP for end-to-end execution, tables and memory; no network."""

import asyncio
import json
import os

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

server = Server("data-fixture")
schema = {"type": "object", "properties": {"rows": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "amount": {"type": "string", "format": "decimal", "x-daeri-unit": "count"}},
    "required": ["id", "amount"], "additionalProperties": False}}}, "required": ["rows"], "additionalProperties": False}


@server.list_tools()
async def tools():
    return [Tool(name="get", description="Read one configured test key", inputSchema={
        "type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False}, outputSchema=schema)]


@server.call_tool(validate_input=False)
async def call(name, arguments):
    if arguments["key"] == "slow":
        await asyncio.sleep(5)
    if arguments["key"] == "failed":
        return CallToolResult(isError=True, content=[TextContent(type="text", text="private provider failure")])
    data = {"rows": [{"id": "one", "amount": os.environ.get("FIXTURE_AMOUNT", "10.00")}]}
    return CallToolResult(structuredContent=data, content=[TextContent(type="text", text=json.dumps(data))])


async def main():
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


asyncio.run(main())
