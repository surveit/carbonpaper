"""Wire the tutorial agent into the generic agent registry, and compose its tool set.

Importing this module is what makes `app.core.agent.registry.build_engine("tutorial", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel

from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT, TUTORIAL_SYSTEM_PROMPT
from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.registry import AgentConfig, register
from app.services import project as project_service, run as run_service
from app.tools.tool_specs import TOOL_SPECS
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

_PROJECT_ID = Annotated[str, "The project name create_tutorial_project returned."]


def make_tutorial_tools(context: BaseModel) -> list[BoundToolSpec]:
    # build_engine validated it against CONFIG.context_schema first.
    assert isinstance(context, TutorialContext)

    def create_tutorial_project() -> TutorialProject:
        return seed_tutorial_project(context)

    # The other three are the app's own tools, same signatures and same descriptions —
    # the tour reads a run exactly as every other surface does. Only the seeding tool
    # above is new, and no editing tool is here at all.
    def run_workflow(
        project_id: str,
        version_id: str = "",
        limits: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        run_id = run_service.start_run(
            project_id, version_id=version_id or None, limits=limits
        )
        status = run_service.read_run_status(project_id, run_id)["status"]
        return {"run_id": run_id, "status": status}

    def get_run_status(project_id: str, run_id: str) -> dict[str, Any]:
        return run_service.read_run_status(project_id, run_id)

    def describe_workflow(project_id: str) -> dict[str, Any]:
        return project_service.describe_workflow(project_id)

    return [
        BoundToolSpec(
            name="create_tutorial_project",
            description=CREATE_TUTORIAL_PROJECT.description,
            fn=create_tutorial_project,
            input_schema={},
            label="Setting up the tutorial project",
        ),
        BoundToolSpec(
            name="run_workflow",
            description=TOOL_SPECS["run_workflow"].description,
            fn=run_workflow,
            input_schema={
                "project_id": _PROJECT_ID,
                "version_id": Annotated[
                    str, "Omit for the project's newest stored version."
                ],
                "limits": Annotated[
                    dict[str, int] | None,
                    'Caps how many rows a stage READS: {"<stage id>": N}.',
                ],
            },
            label="Running the workflow",
        ),
        BoundToolSpec(
            name="get_run_status",
            description=TOOL_SPECS["get_run_status"].description,
            fn=get_run_status,
            input_schema={
                "project_id": _PROJECT_ID,
                "run_id": Annotated[str, "The run id run_workflow returned."],
            },
            label="Checking the run",
        ),
        BoundToolSpec(
            name="describe_workflow",
            description=TOOL_SPECS["describe_workflow"].description,
            fn=describe_workflow,
            input_schema={"project_id": _PROJECT_ID},
            label="Reading the workflow",
        ),
    ]


register("tutorial", CONFIG, make_tutorial_tools)
