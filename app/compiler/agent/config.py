"""Builder for the per-project editing agent: a ClaudeAgentSdkEngine (subscription
CLI, no API key) bound to one project's tools + a system prompt naming it. One
instance per project, cached. Reuses the chat spine (turns.py + store.py + the
FE) verbatim; only the tools + prompt are project-specific."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.compiler.agent.prompt import _system_prompt
from app.web.config import EXAMPLES_DIR

if TYPE_CHECKING:
    from app.agent.sdk_engine import ClaudeAgentSdkEngine

_sdk_engines: dict[str, "ClaudeAgentSdkEngine"] = {}


def get_project_sdk_engine(name: str) -> "ClaudeAgentSdkEngine":
    """Cached subscription (Claude CLI) editing engine for `name`.

    Wraps the project's tools as an in-process SDK-MCP server and drives
    claude_agent_sdk.query() directly, so the subscription backend (no API key)
    can run the tool loop. Construction is lazy w.r.t. the filesystem:
    make_project_tools only binds `EXAMPLES_DIR / name` into tool closures, so
    building the engine never reads the project directory."""
    from app.agent.registry import build_mcp_server
    from app.agent.sdk_engine import ClaudeAgentSdkEngine
    from app.compiler.agent.tools import TOOL_LABELS, TOOL_SCHEMAS, make_project_tools

    if name not in _sdk_engines:
        tools = make_project_tools(name, examples_dir=EXAMPLES_DIR)
        server, allowed, _wrapped = build_mcp_server(tools, TOOL_SCHEMAS)
        _sdk_engines[name] = ClaudeAgentSdkEngine(
            system_prompt=_system_prompt(name),
            mcp_server=server,
            allowed_tools=allowed,
            tool_labels=TOOL_LABELS,
        )
    return _sdk_engines[name]
