"""Wire the tutorial agent into the generic agent registry, and compose its tool set.

Importing this module is what makes `app.core.agent.registry.build_engine("tutorial", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT, TUTORIAL_SYSTEM_PROMPT
from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.registry import AgentConfig, register
from app.tools import shared
from app.tools.tutorial import (
    CREATE_TUTORIAL_PROJECT,
    TutorialContext,
    TutorialProject,
    seed_tutorial_project,
)

CONFIG = AgentConfig(
    system_prompt=TUTORIAL_SYSTEM_PROMPT,
    context_schema=TutorialContext,
    opening_prompt=TUTORIAL_OPENING_PROMPT,
)



def make_tutorial_tools(context: BaseModel) -> list[BoundToolSpec]:
    # build_engine validated it against CONFIG.context_schema first.
    assert isinstance(context, TutorialContext)

    def create_tutorial_project() -> TutorialProject:
        return seed_tutorial_project(context)

    # One new tool. Seeding is the only thing the tour does that no other surface does,
    # and it is the only one that needs this session's context. The rest it REFERENCES.
    return [
        BoundToolSpec(
            name="create_tutorial_project",
            description=CREATE_TUTORIAL_PROJECT.description,
            fn=create_tutorial_project,
            input_schema={},
            label="Setting up the tutorial project",
        ),
        *shared.bind("run_workflow", "get_run_status", "describe_workflow"),
    ]


register("tutorial", CONFIG, make_tutorial_tools)
