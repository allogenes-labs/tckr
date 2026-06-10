"""Universal MCP stdio server adapter.

Exposes the tckr toolkit as a standards-compliant MCP server over
stdin/stdout, using the official `mcp` Python SDK. ANY MCP-compatible client
can spawn it as a subprocess:

- Claude Desktop / Claude Code via `mcp` config
- Cline / Continue.dev
- OpenAI Agents SDK
- LangChain MCP adapter
- Any custom orchestrator that speaks the MCP protocol

Install: `pip install tckr[agent-mcp]`

Run directly: `tckr-mcp`  (console script, installed by pyproject.toml).

Or wire into an MCP client config, e.g. for Claude Desktop:

    {
      "mcpServers": {
        "crypto": {
          "command": "tckr-mcp",
          "env": {
            "COINALYZE_API_KEY": "...",
            "BIRDEYE_API_KEY": "..."
          }
        }
      }
    }
"""
from __future__ import annotations

import asyncio
import json
import logging

from tckr.agent_toolkit.core import TOOLS, augment_description, get_tool

log = logging.getLogger("tckr.agent_toolkit.mcp_stdio")


async def _serve() -> None:
    # Imported lazily so the data layer doesn't need the MCP SDK to install.
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    server: Server = Server("tckr")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=spec.name,
                description=augment_description(spec),
                inputSchema=spec.schema,
            )
            for spec in TOOLS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        # Failures are RAISED, not returned as text: the MCP SDK converts a
        # raised exception into CallToolResult(isError=True), which is the
        # protocol-level error signal clients (and their LLMs) rely on.
        # Returning the message as TextContent would look like a success.
        spec = get_tool(name)
        if spec is None:
            raise ValueError(f"unknown tool: {name}")
        try:
            result = await spec.callable(arguments or {})
        except Exception:
            log.exception("tool %s failed", name)
            raise
        return [TextContent(type="text", text=json.dumps(result, default=str))]

    init_options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main() -> int:
    """Console-script entry point. Configured in pyproject.toml as `tckr-mcp`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_serve())
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
