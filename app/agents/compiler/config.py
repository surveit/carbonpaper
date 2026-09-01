"""Wire the editing agent into the generic agent registry.

Importing this module is what makes `app.core.agent.registry.build_engine("editing", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.compiler.opening import choose_opening_turn
from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
from app.models.terms import render_terms
from app.services.project_record import read_project_name
from app.services import terms as terms_service
from app.tools.editing import EditingContext, build_editing_tools
from app.tools.prompt_fragments import render_link_map
from app.core.agent.registry import AgentConfig, OpeningTurn, register
from app.core.agent.bound_tool import BoundToolSpec


def _render_session_note(context: BaseModel) -> str:
    assert isinstance(context, EditingContext)
    return "\n\n".join(
        part for part in [
            _render_project_binding(context),
            render_link_map(context.base_url),
            _render_project_terms(context),
        ]
        if part
    )


def _render_project_binding(context: EditingContext) -> str:
    # Settled when the chat opened and never reassigned, so it belongs in the prompt.
    if context.project_id is None:
        return _NO_PROJECT_BOUND
    return _PROJECT_BOUND.format(project_id=context.project_id)


_PROJECT_BOUND = """\
# This conversation
It opened in project `{project_id}` — pass that id to any tool wanting one, unless the
reader moves you to another. `get_current_url` says which page they are on now, which
may be a different project or none."""


_NO_PROJECT_BOUND = """\
# This conversation
It opened in no project. There are two ways on: an EXISTING one, which list_projects
names and the reader picks — never guess, and never pick for them — or a NEW one, which
create_project starts from the methodology they give you. Carry the id it returns."""


def _render_project_terms(context: EditingContext) -> str:
    if context.project_id is None:
        return ""
    return render_terms(terms_service.load_terms(context.project_id))


def _render_opening_turn(context: BaseModel) -> OpeningTurn:
    assert isinstance(context, EditingContext)
    if context.task:
        # The task arrives as the reader's first message, so a greeting would talk over it.
        return OpeningTurn(text="")
    name = read_project_name(context.project_id) if context.project_id else None
    return choose_opening_turn(context.opened_on, name)


CONFIG = AgentConfig(
    system_prompt=EDITING_SYSTEM_PROMPT,
    context_schema=EditingContext,
    display_name="Editing",
    # The CLI's own built-in, which loads a deferred MCP tool's schema before first
    # use — it renders in the chat but is not one of this agent's tools.
    extra_tool_labels={"ToolSearch": "Looking up a tool"},
    # The words are read at session start, so an agent edits in whatever the owner
    # has agreed by then rather than in what the process started with. The address is
    # read per turn, so it is where this reader is rather than where the session opened.
    render_session_prompt=_render_session_note,
    # Written, not generated: instant, and it cannot be wrong about a page it never read.
    render_opening_turn=_render_opening_turn,
)


def _build_editing_tools(context: BaseModel) -> list[BoundToolSpec]:
    assert isinstance(context, EditingContext)
    return build_editing_tools(context)


register("editing", CONFIG, _build_editing_tools)
