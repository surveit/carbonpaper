"""Builds the ChatEngine a registered AgentConfig's backend names. An agent registers at import."""
from __future__ import annotations

from typing import Any, Callable, Literal

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.agent.api_engine import AnthropicApiEngine, ApiEffort
from app.core.agent.engine import ChatEngine
from app.core.agent.prices import validate_priced
from app.core.agent.sdk_engine import (
    CLI_MODEL,
    MCP_SERVER_NAME,
    ClaudeAgentSdkEngine,
    ThinkingConfig,
)
from app.core.agent.bound_tool import BoundToolSpec, call_bound_tool
from app.core.ids import ID


class OpeningTurn(BaseModel):
    """An agent's written first turn: what it says, and what it offers as a reply."""

    text: str
    # Empty leaves the reader to type. Replies: an authored turn links to nothing.
    offers: list[str] = []


class CliBackend(BaseModel):
    kind: Literal["cli"] = "cli"
    model: str = CLI_MODEL
    # None keeps the CLI's default; {"type": "disabled"} suits an agent
    # whose prompt scripts its tools.
    thinking: ThinkingConfig | None = None


class ApiBackend(BaseModel):
    kind: Literal["api"] = "api"
    model: str
    effort: ApiEffort

    @field_validator("model")
    @classmethod
    def _validate_priced(cls, model: str) -> str:
        validate_priced(model)
        return model


class AgentConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    system_prompt: str
    backend: CliBackend | ApiBackend = Field(default_factory=CliBackend, discriminator="kind")
    context_schema: type[BaseModel]
    # What a surface calls this agent: a rail header, a new session's title.
    display_name: str
    # Labels for tools this agent does not own — e.g. the CLI's own ToolSearch
    # built-in, which has no BoundToolSpec here but still renders in the chat.
    extra_tool_labels: dict[str, str] = {}
    # The first assistant turn, stored with no AI model call. None waits to be spoken to.
    render_opening_turn: Callable[[BaseModel], OpeningTurn] | None = None
    # Prose only this session's context can supply, appended to system_prompt. What
    # it says is the agent's business; returning "" appends nothing, not a heading
    # over nothing.
    render_session_prompt: Callable[[BaseModel], str] | None = None


# Given a validated context, return the bound tools for one agent.
BuildTools = Callable[[BaseModel], list[BoundToolSpec]]

_registry: dict[str, tuple[AgentConfig, BuildTools]] = {}


def register(agent_id: ID, config: AgentConfig, build_tools: BuildTools) -> None:
    _registry[agent_id] = (config, build_tools)


def is_registered(agent_id: ID) -> bool:
    return agent_id in _registry


def read_display_name(agent_id: ID) -> str:
    config, _build_tools = _registry[agent_id]
    return config.display_name


def render_opening_turn(agent_id: ID, context: dict[str, Any]) -> OpeningTurn | None:
    """None when the agent waits to be spoken to. See AgentConfig.render_opening_turn."""
    config, _build_tools = _registry[agent_id]
    if config.render_opening_turn is None:
        return None
    return config.render_opening_turn(config.context_schema.model_validate(context))


def build_engine(
    agent_id: ID, context: dict[str, Any], *, opening_message: str = ""
) -> ChatEngine:
    config, build_tools = _registry[agent_id]
    ctx = config.context_schema.model_validate(context)
    specs = build_tools(ctx)
    system_prompt = render_system_prompt(config, ctx, opening_message)
    tool_labels = {s.name: s.label for s in specs} | config.extra_tool_labels
    if isinstance(config.backend, ApiBackend):
        return AnthropicApiEngine(
            system_prompt=system_prompt,
            tools=specs,
            tool_labels=tool_labels,
            model=config.backend.model,
            effort=config.backend.effort,
        )
    return _build_cli_engine(config.backend, system_prompt, specs, tool_labels)


def _build_cli_engine(
    backend: CliBackend,
    system_prompt: str,
    specs: list[BoundToolSpec],
    tool_labels: dict[str, str],
) -> ClaudeAgentSdkEngine:
    server, allowed, _wrapped = build_mcp_server(specs)
    return ClaudeAgentSdkEngine(
        system_prompt=system_prompt,
        mcp_server=server,
        allowed_tools=allowed,
        tool_labels=tool_labels,
        model=backend.model,
        thinking=backend.thinking,
    )


# Neither engine replays a stored opening turn, so this is the only way the model sees it.
_OPENED_WITH = "This conversation opened with these words from you, which the reader has read:"


def render_system_prompt(
    config: AgentConfig, context: BaseModel, opening_message: str = ""
) -> str:
    """`opening_message` is what this session already said, so the reader may refer back to it."""
    session_note = (
        config.render_session_prompt(context) if config.render_session_prompt else ""
    )
    opened_with = f"{_OPENED_WITH}\n\n{opening_message}" if opening_message else ""
    return "\n\n".join(
        part for part in (config.system_prompt, session_note, opened_with) if part
    )


# ── claude_agent_sdk MCP wrapping (generic) ──────────────────────────────────
# Mounting a set of bound tools as an in-process MCP server is generic infra: it
# depends only on the specs, not on what any tool does. The server is mounted
# under the fixed generic name MCP_SERVER_NAME. This is also the only module that
# names the SDK: a BoundToolSpec describes a tool in JSON Schema and pydantic, and
# the translation into what THIS provider calls a tool happens here.


def build_mcp_server(
    specs: list[BoundToolSpec],
) -> tuple[McpSdkServerConfig, list[str], list[SdkMcpTool[Any]]]:
    wrapped = [build_sdk_tool(spec) for spec in specs]
    server = create_sdk_mcp_server(MCP_SERVER_NAME, tools=wrapped)
    allowed = [f"mcp__{MCP_SERVER_NAME}__{spec.name}" for spec in specs]
    return server, allowed, wrapped


def build_sdk_tool(spec: BoundToolSpec) -> SdkMcpTool[Any]:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        outcome = await call_bound_tool(spec, args)
        content: dict[str, Any] = {"content": [{"type": "text", "text": outcome.text}]}
        if outcome.is_error:
            content["is_error"] = True
        return content

    return tool(spec.name, spec.description, spec.json_schema)(handler)
