"""Isolated subprocess fixture; no external credentials or networks."""

import asyncio
import json
import os

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

server = Server("resource-fixture")


@server.list_tools()
async def tools():
    mode = os.environ.get("FIXTURE_MODE", "normal")
    return [Tool(name="get_repository_file", description="fixture " + mode,
                 inputSchema={"type": "object", "properties": {
                     "project_id": {"type": "string"}, "ref": {"type": "string"},
                     "file_path": {"type": "string"}, "limit": {"type": "integer", "default": 20}},
                     "required": ["project_id", "ref", "file_path"], "additionalProperties": False},
                 outputSchema={"type": "object", "properties": {"body": {"type": "string"}},
                               "required": ["body"], "additionalProperties": False})]


@server.call_tool(validate_input=False)
async def call(name, arguments):
    if arguments.get("file_path") == "src/slow.py":
        await asyncio.sleep(5)
    if arguments.get("file_path") == "src/fail.py":
        return CallToolResult(content=[TextContent(type="text", text="password=should-not-leak")], isError=True)
    value = {"body": "password=remove-this " + json.dumps(arguments, sort_keys=True)}
    if arguments.get("file_path") == "src/invalid.py":
        value = {"unexpected": 1}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(value))], structuredContent=value)


async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


asyncio.run(main())
