"""Claude Agent SDK adapter.

Builds an in-process MCP server that the Claude Agent SDK can mount via
`ClaudeAgentOptions(mcp_servers={...})`. No subprocess — the SDK and tools
share the same Python process and event loop.

Install: `pip install tckr[agent-claude]`

Usage in a Claude Agent SDK project::

    from tckr.agent_toolkit.adapters.claude_sdk import (
        build_crypto_mcp_server, ALLOWED_TOOL_NAMES, MCP_SERVER_NAME,
    )
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        allowed_tools=ALLOWED_TOOL_NAMES,
        mcp_servers={MCP_SERVER_NAME: build_crypto_mcp_server()},
        max_turns=27,
    )
"""
from __future__ import annotations

import json
import logging
from typing import Any

from tckr.agent_toolkit.core import TOOLS, ToolSpec, augment_description

log = logging.getLogger("tckr.agent_toolkit.claude_sdk")

MCP_SERVER_NAME = "crypto"
TOOL_NAMES = [t.name for t in TOOLS]
ALLOWED_TOOL_NAMES = [f"mcp__{MCP_SERVER_NAME}__{n}" for n in TOOL_NAMES]


def _ok(data: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}


def _err(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _wrap_tool(spec: ToolSpec):
    """Convert a `ToolSpec` to a Claude-SDK-decorated callable.

    The SDK's `@tool` decorator returns a tool object; we generate one per
    `ToolSpec`. Errors are caught and returned as MCP error blocks so the SDK
    loop doesn't crash on a transient upstream hiccup.
    """
    from claude_agent_sdk import tool  # imported lazily so the data layer doesn't need the SDK

    description = augment_description(spec)
    callable_ref = spec.callable
    label = spec.name

    @tool(spec.name, description, spec.schema)
    async def _wrapped(args: dict) -> dict:
        try:
            result = await callable_ref(args)
            return _ok(result)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", label)
            return _err(f"{label} failed: {type(e).__name__}: {e}")

    return _wrapped


def build_crypto_mcp_server():
    """Build the in-memory MCP server config. Reusable across all agent calls."""
    from claude_agent_sdk import create_sdk_mcp_server

    wrapped = [_wrap_tool(spec) for spec in TOOLS]
    return create_sdk_mcp_server(
        name=MCP_SERVER_NAME,
        version="0.1.0",
        tools=wrapped,
    )


__all__ = [
    "build_crypto_mcp_server",
    "ALLOWED_TOOL_NAMES",
    "MCP_SERVER_NAME",
    "TOOL_NAMES",
]
