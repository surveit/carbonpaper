"""Wire the tutorial agent into the generic agent registry, and compose its tool set.

Importing this module is what makes `app.core.agent.registry.build_engine("tutorial", …)`
work; app.main imports it at startup."""

from __future__ import annotations

from pydantic import BaseModel

from app.agents.tutorial.prompt import TUTORIAL_SYSTEM_PROMPT
from app.core.agent.bound_tool import BoundToolSpec, bind_by_signature
from app.core.agent.registry import AgentConfig, register
from app.tools import eval_runs, shared
from app.tools.eval_runs import EvalRunResult
from app.tools.shared import StageOutputRows
from app.tools.tool_specs import bind, read_parameter_prose, read_tool_description
from app.tools.tutorial import (
    CREATE_TUTORIAL_PROJECT,
    TutorialAgentReference,
    TutorialContext,
    seed_tutorial_project,
)

# The tour's first turn, written rather than generated: every reader is welcomed in the
# same words, and the script picks up from the question it ends on.
_TUTORIAL_OPENING = """\
Welcome to Carbon Paper. You write your methodology as prose, an AI agent turns it into a \
workflow of named, typed stages, and every row of the result traces back to the row it \
came from.

And welcome to the tutorial — I'm glad you're here.

I'm going to seed a sample investigation for you to explore.

Ready to get started?"""


def _render_opening_message(context: BaseModel) -> str:
    assert isinstance(context, TutorialContext)
    return _TUTORIAL_OPENING


CONFIG = AgentConfig(
    system_prompt=TUTORIAL_SYSTEM_PROMPT,
    context_schema=TutorialContext,
    render_opening_message=_render_opening_message,
)



def make_tutorial_tools(context: BaseModel) -> list[BoundToolSpec]:
    # build_engine validated it against CONFIG.context_schema first.
    assert isinstance(context, TutorialContext)

    def create_tutorial_project() -> TutorialAgentReference:
        return seed_tutorial_project(context)

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
        bind_by_signature(
            name="create_tutorial_project",
            description=CREATE_TUTORIAL_PROJECT.description,
            fn=create_tutorial_project,
            label="Setting up the tutorial project",
            parameters=CREATE_TUTORIAL_PROJECT.parameters,
        ),
        bind_by_signature(
            name="read_stage_output_rows",
            description=read_tool_description("read_stage_output_rows"),
            fn=read_stage_output_rows,
            label="Reading the stage's rows",
            parameters=read_parameter_prose("read_stage_output_rows"),
        ),
        bind_by_signature(
            name="run_eval",
            description=eval_runs.RUN_EVAL.description,
            fn=run_eval,
            label="Running the eval",
            parameters=eval_runs.RUN_EVAL.parameters,
        ),
        *bind("run_workflow", "get_run_status", "sleep", "read_workflow_summary"),
    ]


register("tutorial", CONFIG, make_tutorial_tools)
