"""Agent registry + engine builder — turns a registered AgentConfig into a
ready-to-run ClaudeAgentSdkEngine. Nothing here knows about any specific agent;
a concrete agent registers itself at import, so its module must be imported first.
"""
from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool
from pydantic import BaseModel, ConfigDict

from app.core.agent.sdk_engine import MCP_SERVER_NAME, ClaudeAgentSdkEngine, ThinkingConfig
from app.core.agent.bound_tool import BoundToolSpec
from app.core.ids import ID


class OpeningTurn(BaseModel):
    """An agent's written first turn: what it says, and what it offers as a reply."""

    text: str
    # Empty leaves the reader to type. Replies: an authored turn links to nothing.
    offers: list[str] = []


class AgentConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    system_prompt: str
    # None leaves the CLI's own default in place. A scripted agent whose replies
    # are short and whose tool sequence the prompt already dictates has nothing
    # for a reasoning block to earn — {"type": "disabled"} skips straight to text.
    thinking: ThinkingConfig | None = None
    model: str = "sonnet"
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
    # Prose for THIS turn, prepended to the reader's message so the cached prefix holds.
    render_turn_note: Callable[[BaseModel], str] | None = None


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
) -> ClaudeAgentSdkEngine:
    config, build_tools = _registry[agent_id]
    ctx = config.context_schema.model_validate(context)
    specs = build_tools(ctx)
    server, allowed, _wrapped = build_mcp_server(specs)
    return ClaudeAgentSdkEngine(
        system_prompt=render_system_prompt(config, ctx, opening_message),
        mcp_server=server,
        allowed_tools=allowed,
        tool_labels={s.name: s.label for s in specs} | config.extra_tool_labels,
        model=config.model,
        thinking=config.thinking,
        turn_note=config.render_turn_note(ctx) if config.render_turn_note else "",
    )


# A turn is driven by the reader's message alone — the engine drops message_history and
# the store replays nothing — so the session's first message reaches the model only here.
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
        try:
            result = spec.fn(**spec.parse_arguments(args))
            # An async tool is one that WAITS (get_run_status holding open on a
            # running run); awaiting it here is what keeps the app answering
            # everything else meanwhile.
            if inspect.isawaitable(result):
                result = await result
            return as_tool_content(result)
        except Exception as exc:  # noqa: BLE001 — tool boundary: any tool failure is surfaced to the model as an error, never swallowed or faked
            return {
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "is_error": True,
            }

    return tool(spec.name, spec.description, spec.json_schema)(handler)


def as_tool_content(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        text = value
    else:
        # by_alias + exclude_none so a model carrying Stage(s) (e.g. a draft view)
        # comes back to the agent in the SAME spec-dict form it writes stages
        # in — aliased (`schema`, not `table_schema`) and without the unset-optional
        # nulls, matching app.models.stage_to_spec_dict. Additive for every other
        # model-returning tool: an alias-free model (DraftView, DraftEdit,
        # SaveResult, ...) dumps equivalently (a dropped null re-parses as its
        # default).
        dumpable = (
            value.model_dump(mode="json", by_alias=True, exclude_none=True)
            if isinstance(value, BaseModel)
            else value
        )
        text = json.dumps(dumpable, default=str, indent=2)
    return {"content": [{"type": "text", "text": text}]}
