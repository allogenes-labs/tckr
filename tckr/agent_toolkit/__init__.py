"""tckr agent toolkit — platform-neutral.

The toolkit exposes ~20 read-only research tools backed by the tckr
package. The same `ToolSpec` registry feeds every supported agent platform:

    Adapter                              Optional dep              Use it for
    -----------------------------------  ------------------------  ---------------------------
    adapters.claude_sdk                  tckr[agent-claude]  Claude Agent SDK in-process MCP server
    adapters.mcp_stdio                   tckr[agent-mcp]     Universal MCP stdio server (any MCP client)
                                                                   Console script: tckr-mcp
    adapters.openai                      tckr[agent-openai]  OpenAI / Anthropic chat-completions function calling
    adapters.langchain                   tckr[agent-langchain]  LangChain StructuredTool wrappers

Each adapter consumes the same `core.TOOLS` list and the same
`tckr.registry` tier tags, so tool descriptions stay consistent across
platforms and there is one source of truth to maintain.

Adapter-neutral exports live at the package root:

    TOOLS              — list[ToolSpec], the source of truth
    ToolSpec           — dataclass with .name / .description / .module / .schema / .callable
    augment_description — adds the registry tier tag to a raw description
    render_tools_doc   — compact prompt-injection text listing all tools, grouped by tier
"""
from __future__ import annotations

from tckr.agent_toolkit.core import (
    TOOLS,
    ToolSpec,
    augment_description,
    render_tools_doc,
)

__all__ = [
    "TOOLS",
    "ToolSpec",
    "augment_description",
    "render_tools_doc",
]
