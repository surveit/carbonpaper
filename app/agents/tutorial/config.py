"""Wire the tutorial agent into the generic agent registry.

Importing this module is what makes `app.core.agent.registry.build_engine("tutorial", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT, TUTORIAL_SYSTEM_PROMPT
from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.registry import AgentConfig, register
from app.tools.tutorial import TutorialContext, make_tutorial_tools

CONFIG = AgentConfig(
    system_prompt=TUTORIAL_SYSTEM_PROMPT,
    context_schema=TutorialContext,
    opening_prompt=TUTORIAL_OPENING_PROMPT,
)


def _build_tutorial_tools(context: BaseModel) -> list[BoundToolSpec]:
    # build_engine validated it against CONFIG.context_schema first.
    assert isinstance(context, TutorialContext)
    return make_tutorial_tools(context)


register("tutorial", CONFIG, _build_tutorial_tools)
