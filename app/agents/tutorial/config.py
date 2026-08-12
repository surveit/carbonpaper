"""Wire the tutorial agent into the generic agent registry, and compose its tool set.

Importing this module is what makes `app.core.agent.registry.build_engine("tutorial", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel

from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT, TUTORIAL_SYSTEM_PROMPT
from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.registry import AgentConfig, register
from app.tools import shared
from app.tools.tutorial import (
    CREATE_TUTORIAL_PROJECT,
    READ_ROW_LINEAGE_LINKS,
    StageRowLineage,
    TutorialContext,
    TutorialProject,
    read_row_lineage_links,
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

    def read_lineage_links(project_id: str, run_id: str, stage_id: str) -> StageRowLineage:
        return read_row_lineage_links(context, project_id, run_id, stage_id)

    # Two new tools, both because they close over this session's base_url — seeding builds
    # the tour's links from it, and a lineage link is handed over whole rather than joined
    # by the model. The rest it REFERENCES.
    return [
        BoundToolSpec(
            name="create_tutorial_project",
            description=CREATE_TUTORIAL_PROJECT.description,
            fn=create_tutorial_project,
            input_schema={},
            label="Setting up the tutorial project",
        ),
        BoundToolSpec(
            name="read_row_lineage_links",
            description=READ_ROW_LINEAGE_LINKS.description,
            fn=read_lineage_links,
            input_schema={
                "project_id": Annotated[str, "The project the run belongs to."],
                "run_id": Annotated[str, "The run id run_workflow returned."],
                "stage_id": Annotated[str, "The stage whose output rows you want."],
            },
            label="Finding rows to trace",
        ),
        *shared.bind("run_workflow", "get_run_status", "sleep", "describe_workflow"),
    ]


register("tutorial", CONFIG, make_tutorial_tools)
