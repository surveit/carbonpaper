"""Agent registry + engine builder — turns a registered AgentConfig into a
ready-to-run ClaudeAgentSdkEngine. Nothing here knows about any specific agent;
a concrete agent registers itself at import, so its module must be imported first.
"""
from __future__ import annotations

from typing import Any, Callable

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server
from pydantic import BaseModel, ConfigDict

from app.core.agent.sdk_engine import MCP_SERVER_NAME, ClaudeAgentSdkEngine
from app.core.agent.bound_tool import BoundToolSpec


class AgentConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    system_prompt: str
    model: str = "sonnet"
    context_schema: type[BaseModel]
    # Labels for tools this agent does not own — e.g. the CLI's own ToolSearch
    # built-in, which has no BoundToolSpec here but still renders in the chat.
    extra_tool_labels: dict[str, str] = {}


# Given a validated context, return the bound tools for one agent.
BuildTools = Callable[[BaseModel], list[BoundToolSpec]]

_registry: dict[str, tuple[AgentConfig, BuildTools]] = {}


def register(agent_id: str, config: AgentConfig, build_tools: BuildTools) -> None:
    _registry[agent_id] = (config, build_tools)


def build_engine(agent_id: str, context: dict[str, Any]) -> ClaudeAgentSdkEngine:
    config, build_tools = _registry[agent_id]
    ctx = config.context_schema.model_validate(context)
    specs = build_tools(ctx)
    server, allowed, _wrapped = build_mcp_server(specs)
    return ClaudeAgentSdkEngine(
        system_prompt=config.system_prompt,
        mcp_server=server,
        allowed_tools=allowed,
        tool_labels={s.name: s.label for s in specs} | config.extra_tool_labels,
        model=config.model,
    )


# ── claude_agent_sdk MCP wrapping (generic) ──────────────────────────────────
# Mounting a set of bound tools as an in-process MCP server is generic infra: it
# depends only on the specs, not on what any tool does. The server is mounted
# under the fixed generic name MCP_SERVER_NAME.


def build_mcp_server(
    specs: list[BoundToolSpec],
) -> tuple[McpSdkServerConfig, list[str], list[SdkMcpTool[Any]]]:
    wrapped = [spec.as_sdk_tool() for spec in specs]
    server = create_sdk_mcp_server(MCP_SERVER_NAME, tools=wrapped)
    allowed = [f"mcp__{MCP_SERVER_NAME}__{spec.name}" for spec in specs]
    return server, allowed, wrapped
