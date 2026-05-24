"""OpenAI function-calling adapter.

Exposes the tckr toolkit in the shape OpenAI's `chat.completions.create`
(and the Agents SDK) expects under the `tools` parameter, plus an
`execute_tool(name, args)` dispatcher that routes a model's tool call back to
the underlying async function.

Install: `pip install tckr[agent-openai]`

The `openai` dep is technically optional here — we only need the dict shapes,
not the SDK client. Importing `openai` is left to the caller so the same
adapter works for Anthropic's tool-use schema (close-enough), Mistral, Groq,
DeepSeek, Together, OpenRouter, etc.

Typical use::

    from openai import AsyncOpenAI
    from tckr.agent_toolkit.adapters.openai import (
        get_openai_tools, execute_tool,
    )

    client = AsyncOpenAI()
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "What's BTC funding right now?"}],
        tools=get_openai_tools(),
    )
    # ... on a tool_calls response:
    for call in response.choices[0].message.tool_calls:
        args = json.loads(call.function.arguments)
        result = await execute_tool(call.function.name, args)
        # feed result back into the conversation as a tool message
"""
from __future__ import annotations

import json
import logging
from typing import Any

from tckr.agent_toolkit.core import TOOLS, augment_description, get_tool

log = logging.getLogger("tckr.agent_toolkit.openai")


def get_openai_tools() -> list[dict]:
    """Return the toolkit in OpenAI's `tools=` parameter shape.

    Each entry::

        {
          "type": "function",
          "function": {
            "name": "<tool_name>",
            "description": "<tier-tagged description>",
            "parameters": <JSON Schema>,
          }
        }
    """
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": augment_description(spec),
                "parameters": spec.schema,
            },
        }
        for spec in TOOLS
    ]


def get_anthropic_tools() -> list[dict]:
    """Return the toolkit in Anthropic Messages API `tools=` shape.

    Same registry, slightly different envelope. Use when calling Anthropic
    directly (not via the Agent SDK)::

        {
          "name": "<tool_name>",
          "description": "<tier-tagged description>",
          "input_schema": <JSON Schema>,
        }
    """
    return [
        {
            "name": spec.name,
            "description": augment_description(spec),
            "input_schema": spec.schema,
        }
        for spec in TOOLS
    ]


async def execute_tool(name: str, args: dict | str | None = None) -> Any:
    """Dispatch a tool call to the underlying function.

    `args` accepts a dict (already parsed) OR a JSON string (raw from a model
    tool_calls response). Returns the raw result on success or
    `{"error": "..."}` on failure — never raises.
    """
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else {}
        except json.JSONDecodeError as e:
            return {"error": f"invalid JSON arguments: {e}"}
    args = args or {}

    spec = get_tool(name)
    if spec is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return await spec.callable(args)
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


__all__ = ["get_openai_tools", "get_anthropic_tools", "execute_tool"]
