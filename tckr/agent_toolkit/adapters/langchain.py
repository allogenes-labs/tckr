"""LangChain adapter — wrap each tckr tool as a `StructuredTool`.

Install: `pip install tckr[agent-langchain]`

Use with any LangChain agent (LangGraph, AgentExecutor, etc.)::

    from langchain_anthropic import ChatAnthropic
    from langgraph.prebuilt import create_react_agent
    from tckr.agent_toolkit.adapters.langchain import get_langchain_tools

    tools = get_langchain_tools()
    llm = ChatAnthropic(model="claude-sonnet-4-6")
    agent = create_react_agent(llm, tools)
    result = await agent.ainvoke(
        {"messages": [("user", "What's the BTC funding APR right now?")]}
    )
"""
from __future__ import annotations

import logging
from typing import Any

from tckr.agent_toolkit.core import TOOLS, ToolSpec, augment_description

log = logging.getLogger("tckr.agent_toolkit.langchain")


def _json_schema_to_pydantic(name: str, schema: dict):
    """Convert a JSON Schema dict into a Pydantic model class.

    LangChain's `StructuredTool` infers argument validation from a Pydantic
    `args_schema`. We build one on the fly from the same JSON Schema the other
    adapters use, so the source of truth stays one place.

    Supports the subset of JSON Schema we actually use: object with `properties`
    of {string, integer, number, boolean}, plus `required` and `default`.
    """
    from pydantic import Field, create_model

    type_map = {
        "string":  str,
        "integer": int,
        "number":  float,
        "boolean": bool,
    }
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for field_name, prop in properties.items():
        py_type = type_map.get(prop.get("type"), Any)
        description = prop.get("description", "")
        if field_name in required:
            fields[field_name] = (py_type, Field(..., description=description))
        else:
            default = prop.get("default")
            fields[field_name] = (py_type | None, Field(default, description=description))
    return create_model(f"{name}_Args", **fields)


def _wrap(spec: ToolSpec):
    from langchain_core.tools import StructuredTool

    callable_ref = spec.callable
    label = spec.name
    args_schema = _json_schema_to_pydantic(spec.name, spec.schema)

    async def _coro(**kwargs):
        try:
            # Drop None values so each tool's own defaults apply.
            args = {k: v for k, v in kwargs.items() if v is not None}
            return await callable_ref(args)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", label)
            return {"error": f"{type(e).__name__}: {e}"}

    return StructuredTool.from_function(
        coroutine=_coro,
        name=spec.name,
        description=augment_description(spec),
        args_schema=args_schema,
    )


def get_langchain_tools() -> list:
    """Return the toolkit as a list of LangChain `StructuredTool` instances."""
    return [_wrap(spec) for spec in TOOLS]


__all__ = ["get_langchain_tools"]
