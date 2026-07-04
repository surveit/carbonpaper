"""project_agent.py — builds the per-project editing agent: a ChatEngine bound to
one project's tools + a system prompt naming it. One agent per project, cached.
Reuses the chat spine (turns.py + store.py + the FE) verbatim; only the tools +
prompt are project-specific."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.chat.engine import ChatEngine
from app.chat.project_tools import make_project_tools
from app.web.config import EXAMPLES_DIR

SYSTEM_PROMPT_TEMPLATE = (
    "You help a journalist author and refine the project '{name}' — a workflow of "
    "typed stages. Read before you edit (describe_workflow, read_stage). Every edit "
    "is validated and lands as UNREVIEWED (amber) for a human to approve — you "
    "cannot approve nodes. Snapshot a version (create_version) before regenerating "
    "from scratch. The source document stays on disk: fetch_document returns an "
    "outline, read_section/grep_doc return slices, compile_workflow reads the path. "
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
