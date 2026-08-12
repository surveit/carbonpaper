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
    # Set to make the agent speak first: an empty session runs one turn on this
    # prompt with no reader message. It is never shown or stored as one, so the
    # reader is not credited with words they did not type. None = wait to be spoken to.
    opening_prompt: str | None = None
    # Prose only this session's context can supply, appended to system_prompt. What
    # it says is the agent's business; returning "" appends nothing, not a heading
    # over nothing.
    render_session_prompt: Callable[[BaseModel], str] | None = None


# Given a validated context, return the bound tools for one agent.
BuildTools = Callable[[BaseModel], list[BoundToolSpec]]

_registry: dict[str, tuple[AgentConfig, BuildTools]] = {}


def register(agent_id: str, config: AgentConfig, build_tools: BuildTools) -> None:
    _registry[agent_id] = (config, build_tools)


def opening_prompt(agent_id: str) -> str | None:
    """None when this agent waits to be spoken to. See AgentConfig.opening_prompt."""
    config, _build_tools = _registry[agent_id]
    return config.opening_prompt


def build_engine(agent_id: str, context: dict[str, Any]) -> ClaudeAgentSdkEngine:
    config, build_tools = _registry[agent_id]
    ctx = config.context_schema.model_validate(context)
    specs = build_tools(ctx)
    server, allowed, _wrapped = build_mcp_server(specs)
    return ClaudeAgentSdkEngine(
        system_prompt=render_system_prompt(config, ctx),
        mcp_server=server,
        allowed_tools=allowed,
        tool_labels={s.name: s.label for s in specs} | config.extra_tool_labels,
        model=config.model,
    )


def render_system_prompt(config: AgentConfig, context: BaseModel) -> str:
    if config.render_session_prompt is None:
        return config.system_prompt
    appended = config.render_session_prompt(context)
    if not appended:
        return config.system_prompt
    return f"{config.system_prompt}\n\n{appended}"


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
