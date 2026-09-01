"""Wire the editing agent into the generic agent registry.

Importing this module is what makes `app.core.agent.registry.build_engine("editing", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from pydantic import BaseModel

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
        part for part in [render_link_map(context.base_url), _render_project_terms(context)]
        if part
    )


def _render_project_terms(context: EditingContext) -> str:
    if context.project_id is None:
        return ""
    return render_terms(terms_service.load_terms(context.project_id))


def _render_opening_turn(context: BaseModel) -> OpeningTurn:
    assert isinstance(context, EditingContext)
    if context.project_id is None:
        return OpeningTurn(text=_BLANK_CHAT_OPENING)
    if context.task:
        # The task arrives as the reader's first message, so a greeting would talk over it.
        return OpeningTurn(text="")
    return OpeningTurn(text=_PROJECT_OPENING.format(name=read_project_name(context.project_id)))


# `POST /chat/new` — nothing is bound yet, so the message is the three ways in, numbered
# so a reply of "the first one" names one of them.
_BLANK_CHAT_OPENING = """\
I build the workflows in Carbon Paper — the stages that turn your data into a result \
someone else can check.

Three ways to start:

1. Upload your data and describe the investigation you want to run on it.
2. Upload a methodology document, and the input data it is meant to run on.
3. Describe changes you want made to a project that already exists.

Attach a file with the paperclip below, or drop one anywhere on this conversation."""


# `/project/<id>/edit-agent` — the reader already has a project, so the offer is what
# can be done TO one. Every line is a tool this agent holds; publishing is not one.
_PROJECT_OPENING = """\
You're in {name}. I edit its workflow, and show you what the edit does:

1. Add, edit or remove stages.
2. Run it — all of it, or a slice of rows as a test — and read the rows each stage produced.
3. Save the result as a version, with a walkthrough for whoever reviews it.

Publishing a version stays yours. Say what you want changed."""


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
    # Written, not generated: the same words for every reader, and the model is told
    # them, so "the first one" resolves to a numbered line above.
    render_opening_turn=_render_opening_turn,
)


def _build_editing_tools(context: BaseModel) -> list[BoundToolSpec]:
    assert isinstance(context, EditingContext)
    return build_editing_tools(context)


register("editing", CONFIG, _build_editing_tools)
