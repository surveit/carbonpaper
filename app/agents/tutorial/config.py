"""Wire the tutorial agent into the generic agent registry, and compose its tool set.

Importing this module is what makes `app.core.agent.registry.build_engine("tutorial", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.tutorial.prompt import TUTORIAL_OPENING_PROMPT, TUTORIAL_SYSTEM_PROMPT
from app.core.agent.bound_tool import BoundToolSpec
from app.core.agent.registry import AgentConfig, register
from app.tools import eval_runs, shared
from app.tools.eval_runs import EvalRunResult
from app.tools.shared import StageOutputRows
from app.tools.tool_specs import TOOL_SPECS
from app.tools.tutorial import (
    CREATE_TUTORIAL_PROJECT,
    OPEN_EDITING_CHAT,
    OPEN_EDITING_CHAT_SCHEMA,
    EditingChat,
    TutorialContext,
    TutorialProject,
    open_editing_chat,
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

    def open_editing_chat_here(project_id: str) -> EditingChat:
        return open_editing_chat(context, project_id)

    def read_stage_output_rows(
        project_id: str, run_id: str, stage_id: str, limit: int | None = None, offset: int = 0
    ) -> StageOutputRows:
        return shared.read_stage_output_rows(
            project_id, run_id, stage_id, limit, offset, base_url=context.base_url.rstrip("/")
        )

    def run_eval(
        project_id: str, eval_id: str, version_id: str | None = None
    ) -> EvalRunResult:
        return eval_runs.run_eval(
            project_id, eval_id, version_id, base_url=context.base_url.rstrip("/")
        )

    # One new tool. Seeding is the only thing the tour does that no other surface does.
    # The row reader and the eval runner are the shared ones, wrapped only because the
    # tour's reader CLICKS what comes back, so the links carry this session's base_url
    # rather than being root-relative.
    return [
        BoundToolSpec(
            name="create_tutorial_project",
            description=CREATE_TUTORIAL_PROJECT.description,
            fn=create_tutorial_project,
            input_schema={},
            label="Setting up the tutorial project",
        ),
        BoundToolSpec(
            name="read_stage_output_rows",
            description=TOOL_SPECS["read_stage_output_rows"].description,
            fn=read_stage_output_rows,
            input_schema=shared.schema_of("read_stage_output_rows"),
            label="Reading the stage's rows",
        ),
        BoundToolSpec(
            name="open_editing_chat",
            description=OPEN_EDITING_CHAT.description,
            fn=open_editing_chat_here,
            input_schema=OPEN_EDITING_CHAT_SCHEMA,
            label="Opening a chat with the editing agent",
        ),
        BoundToolSpec(
            name="run_eval",
            description=eval_runs.RUN_EVAL.description,
            fn=run_eval,
            input_schema=eval_runs.RUN_EVAL_SCHEMA,
            label="Running the eval",
        ),
        *shared.bind("run_workflow", "get_run_status", "sleep", "describe_workflow"),
    ]


register("tutorial", CONFIG, make_tutorial_tools)
