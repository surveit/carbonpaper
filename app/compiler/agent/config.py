"""Builders for the per-project editing agent: a ChatEngine (PydanticAI, API-key
backend) or a ClaudeAgentSdkEngine (subscription CLI, no API key), each bound to
one project's tools + a system prompt naming it. One instance per project, cached.
Reuses the chat spine (turns.py + store.py + the FE) verbatim; only the tools +
prompt are project-specific."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.chat.engine import ChatEngine
from app.compiler.agent.prompt import _system_prompt
from app.compiler.agent.tools import make_project_tools
from app.web.config import EXAMPLES_DIR

if TYPE_CHECKING:
    from app.chat.sdk_engine import ClaudeAgentSdkEngine

_agents: dict[str, ChatEngine] = {}


def build_project_agent(name: str, *, examples_dir: Path = EXAMPLES_DIR, model: Any = None) -> ChatEngine:
    """Construct a fresh editing agent for `name` with only that project's tools
    and a prompt naming it. model=None lets ChatEngine pick the configured backend;
    pass a model in tests to stay offline."""
    return ChatEngine(
        system_prompt=_system_prompt(name),
        tools=make_project_tools(name, examples_dir=examples_dir),
        model=model,
    )


def get_project_agent(name: str) -> ChatEngine:
    """Cached editing agent for `name` (built once per process)."""
    if name not in _agents:
        _agents[name] = build_project_agent(name)
    return _agents[name]


_sdk_engines: dict[str, "ClaudeAgentSdkEngine"] = {}


def get_project_sdk_engine(name: str) -> "ClaudeAgentSdkEngine":
    """Cached subscription (Claude CLI) editing engine for `name`.

    Wraps the project's tools as an in-process SDK-MCP server and drives
    claude_agent_sdk.query() directly, so the subscription backend (no API key)
    can run the tool loop. Reuses the same system prompt so the agent's behavior
    matches the PydanticAI project agent. Construction is lazy w.r.t. the
    filesystem: make_project_tools only binds `EXAMPLES_DIR / name` into tool
    closures, so building the engine never reads the project directory."""
    from app.chat.sdk_engine import ClaudeAgentSdkEngine
    from app.compiler.agent.tools import build_project_mcp_server

    if name not in _sdk_engines:
        server, allowed, _tools = build_project_mcp_server(name, examples_dir=EXAMPLES_DIR)
        _sdk_engines[name] = ClaudeAgentSdkEngine(
            system_prompt=_system_prompt(name),
            mcp_server=server,
            allowed_tools=allowed,
        )
    return _sdk_engines[name]
