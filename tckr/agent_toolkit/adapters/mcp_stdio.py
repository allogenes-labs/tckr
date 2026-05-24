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
        spec = get_tool(name)
        if spec is None:
            return [TextContent(type="text", text=f"unknown tool: {name}")]
        try:
            result = await spec.callable(arguments or {})
            return [TextContent(type="text", text=json.dumps(result, default=str))]
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", name)
            return [TextContent(type="text", text=f"{name} failed: {type(e).__name__}: {e}")]

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
