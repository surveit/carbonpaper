"""Wire the editing agent into the generic agent registry.

Importing this module is what makes `app.core.agent.registry.build_engine("editing", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
from app.models.terms import render_terms
from app.services import terms as terms_service
from app.tools.editing import EditingContext, make_editing_tools
from app.core.agent.registry import AgentConfig, register
from app.core.agent.bound_tool import BoundToolSpec


def _render_project_terms(context: BaseModel) -> str:
    assert isinstance(context, EditingContext)
    if context.project_id is None:
        return ""
    return render_terms(terms_service.load_terms(context.project_id))


CONFIG = AgentConfig(
    system_prompt=EDITING_SYSTEM_PROMPT,
    context_schema=EditingContext,
    # The CLI's own built-in, which loads a deferred MCP tool's schema before first
    # use — it renders in the chat but is not one of this agent's tools.
    extra_tool_labels={"ToolSearch": "Looking up a tool"},
    # The words are read at session start, so an agent edits in whatever the owner
    # has agreed by then rather than in what the process started with.
    render_session_prompt=_render_project_terms,
)


def _build_editing_tools(context: BaseModel) -> list[BoundToolSpec]:
    assert isinstance(context, EditingContext)
    return make_editing_tools(context)


register("editing", CONFIG, _build_editing_tools)
