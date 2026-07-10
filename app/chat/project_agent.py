"""project_agent.py — builds the per-project editing agent: a ChatEngine bound to
one project's tools + a system prompt naming it. One agent per project, cached.
Reuses the chat spine (turns.py + store.py + the FE) verbatim; only the tools +
prompt are project-specific."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.chat.engine import ChatEngine
from app.chat.project_tools import make_project_tools
from app.web.config import EXAMPLES_DIR

if TYPE_CHECKING:
    from app.chat.sdk_engine import SdkAgentEngine

SYSTEM_PROMPT_TEMPLATE = (
    "You help a journalist author and refine the project '{name}' — a workflow of "
    "typed stages. Read before you edit (describe_workflow, read_stage). Prefer "
    "small, targeted changes: edit_stage and add_stage. Every edit is validated and "
    "lands as UNREVIEWED (amber) for a human to approve — you cannot approve nodes. "
    "compile_workflow REBUILDS the entire workflow from the conversation so far (a "
    "full reset): use it only when the user explicitly asks to rebuild from scratch, "
    "warn them first that it replaces everything and takes a few minutes, and "
    "snapshot a version (create_version) first if any node carries review work. "
    "Never invent a column, source, model, or value — if you lack it, ask."
)

_agents: dict[str, ChatEngine] = {}


def build_project_agent(name: str, *, examples_dir: Path = EXAMPLES_DIR, model: Any = None) -> ChatEngine:
    """Construct a fresh editing agent for `name` with only that project's tools
    and a prompt naming it. model=None lets ChatEngine pick the configured backend;
    pass a model in tests to stay offline."""
    return ChatEngine(
        system_prompt=SYSTEM_PROMPT_TEMPLATE.format(name=name),
        tools=make_project_tools(name, examples_dir=examples_dir),
        model=model,
    )


def get_project_agent(name: str) -> ChatEngine:
    """Cached editing agent for `name` (built once per process)."""
    if name not in _agents:
        _agents[name] = build_project_agent(name)
    return _agents[name]


_sdk_engines: dict[str, "SdkAgentEngine"] = {}


def get_project_sdk_engine(name: str) -> "SdkAgentEngine":
    """Cached subscription (Claude CLI) editing engine for `name`.

    Wraps the same 9 project tools as an in-process SDK-MCP server and drives
    claude_agent_sdk.query() directly, so the subscription backend (no API key)
    can run the tool loop. Reuses SYSTEM_PROMPT_TEMPLATE so the agent's behavior
    matches the PydanticAI project agent. Construction is lazy w.r.t. the
    filesystem: make_project_tools only binds `EXAMPLES_DIR / name` into tool
    closures, so building the engine never reads the project directory."""
    from app.chat.sdk_engine import SdkAgentEngine
    from app.chat.sdk_tools import build_project_mcp_server

    if name not in _sdk_engines:
        server, allowed, _tools = build_project_mcp_server(name, examples_dir=EXAMPLES_DIR)
        _sdk_engines[name] = SdkAgentEngine(
            system_prompt=SYSTEM_PROMPT_TEMPLATE.format(name=name),
            mcp_server=server,
            allowed_tools=allowed,
        )
    return _sdk_engines[name]
