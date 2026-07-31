"""Wire the editing agent into the generic agent registry.

Importing this module is what makes `app.core.agent.registry.build_engine("editing", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from app.agents.compiler.prompt import EDITING_SYSTEM_PROMPT
from app.agents.compiler.tools import (
    TOOL_DESCRIPTIONS,
    TOOL_LABELS,
    TOOL_SCHEMAS,
    EditingContext,
    make_editing_tools,
)
from app.core.agent.registry import AgentConfig, register

CONFIG = AgentConfig(
    system_prompt=EDITING_SYSTEM_PROMPT,
    tool_schemas=TOOL_SCHEMAS,
    tool_descriptions=TOOL_DESCRIPTIONS,
    tool_labels=TOOL_LABELS,
    context_schema=EditingContext,
)


def _build_editing_tools(context: BaseModel) -> list[Callable[..., Any]]:
    # build_engine hands the context back as the base type; it is an EditingContext
    # because that is CONFIG.context_schema and build_engine validates against it.
    assert isinstance(context, EditingContext)
    return make_editing_tools(context)


register("editing", CONFIG, _build_editing_tools)
