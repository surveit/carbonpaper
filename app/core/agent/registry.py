"""Agent registry + engine builder — the generic wiring that turns a registered
AgentConfig into a ready-to-run ClaudeAgentSdkEngine.

An AgentConfig is the static description of one agent: its system prompt, model,
the tools' input schemas + display labels, and the pydantic model its opaque
context validates against. A registered agent also supplies a `build_tools`
callable that, given a validated context, returns the in-process tool callables.
`build_engine` validates the caller's context, builds the tools, wraps them as an
in-process SDK-MCP server, and hands the whole thing to the engine. Nothing here
knows about any specific agent; a concrete agent registers itself at import.
"""
from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from claude_agent_sdk import (
    McpSdkServerConfig,
    SdkMcpTool,
    create_sdk_mcp_server,
    tool,
)
from pydantic import BaseModel, ConfigDict

from app.core.agent.sdk_engine import MCP_SERVER_NAME, ClaudeAgentSdkEngine


class AgentConfig(BaseModel):
    """The static description of one agent the registry can build an engine for."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    system_prompt: str
    tool_schemas: dict[str, dict[str, object]]
    tool_labels: dict[str, str]
    model: str = "sonnet"
    context_schema: type[BaseModel]


# Given a validated context, return the in-process tool callables for one agent.
BuildTools = Callable[[BaseModel], list[Callable[..., Any]]]

_registry: dict[str, tuple[AgentConfig, BuildTools]] = {}


def register(agent_id: str, config: AgentConfig, build_tools: BuildTools) -> None:
    """Register an agent under `agent_id` so `build_engine(agent_id, context)` can
    construct it. Called at import by the module that owns the agent."""
    _registry[agent_id] = (config, build_tools)


def build_engine(agent_id: str, context: dict[str, Any]) -> ClaudeAgentSdkEngine:
    """Build the engine for a registered agent, binding it to `context`.

    Looks up the (config, build_tools) pair (a KeyError for an unregistered
    agent_id fails loudly rather than silently), validates the opaque context
    against the agent's context_schema, builds that context's tools, wraps them as
    an in-process SDK-MCP server, and returns the ready engine."""
    config, build_tools = _registry[agent_id]
    ctx = config.context_schema.model_validate(context)
    tools = build_tools(ctx)
    server, allowed, _wrapped = build_mcp_server(tools, config.tool_schemas)
    return ClaudeAgentSdkEngine(
        system_prompt=config.system_prompt,
        mcp_server=server,
        allowed_tools=allowed,
        tool_labels=config.tool_labels,
        model=config.model,
    )


# ── claude_agent_sdk MCP wrapping (generic) ──────────────────────────────────
# Wrapping a set of plain callables as an in-process MCP server is generic infra:
# it depends only on the callables and their input schemas, not on what any tool
# does. The server is mounted under the fixed generic name MCP_SERVER_NAME.


def build_mcp_server(
    tools: list[Callable[..., Any]],
    tool_schemas: dict[str, dict[str, object]],
) -> tuple[McpSdkServerConfig, list[str], list[SdkMcpTool[Any]]]:
    """Wrap tool callables as an in-process SDK-MCP server.

    Returns `(server, allowed_tool_names, wrapped_tools)`:
    - `server` goes into `ClaudeAgentOptions.mcp_servers`;
    - each allowed name is `f"mcp__{MCP_SERVER_NAME}__{fn.__name__}"`;
    - `wrapped_tools` is the `SdkMcpTool` list (the server dict exposes no public
      accessor for it) so callers can invoke a handler directly.
    """
    wrapped = [_wrap(fn, tool_schemas[fn.__name__]) for fn in tools]
    server = create_sdk_mcp_server(MCP_SERVER_NAME, tools=wrapped)
    allowed = [f"mcp__{MCP_SERVER_NAME}__{fn.__name__}" for fn in tools]
    return server, allowed, wrapped


def _wrap(fn: Callable[..., Any], schema: dict[str, object]) -> SdkMcpTool[Any]:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _as_content(fn(**args))
        except Exception as exc:  # noqa: BLE001 — tool boundary: any tool failure is surfaced to the model as an error, never swallowed or faked
            return {
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "is_error": True,
            }

    # The full docstring is the model-facing description — it carries the usage
    # guidance (read before edit, pass the full stage JSON, id must match). Using
    # only the first line would drop exactly what the model needs.
    description = inspect.getdoc(fn) or fn.__name__
    return tool(fn.__name__, description, schema)(handler)


def _as_content(value: object) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, default=str, indent=2)
    return {"content": [{"type": "text", "text": text}]}
