"""Wire the editing agent into the generic agent registry.

Importing this module is what makes `app.core.agent.registry.build_engine("editing", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
from app.agents.compiler.tools import EditingContext, make_editing_tools
from app.core.agent.registry import AgentConfig, register
from app.core.agent.tool_spec import BoundToolSpec

CONFIG = AgentConfig(
    system_prompt=EDITING_SYSTEM_PROMPT,
    context_schema=EditingContext,
    # The CLI's own built-in, which loads a deferred MCP tool's schema before first
    # use — it renders in the chat but is not one of this agent's tools.
    extra_tool_labels={"ToolSearch": "Looking up a tool"},
)


def _build_editing_tools(context: BaseModel) -> list[BoundToolSpec]:
    # build_engine hands the context back as the base type; it is an EditingContext
    # because that is CONFIG.context_schema and build_engine validates against it.
    assert isinstance(context, EditingContext)
    return make_editing_tools(context)


register("editing", CONFIG, _build_editing_tools)
