"""Running one of a project's evals, as a tool: the score, and the page it is read on.

The same three calls the eval page's Run button makes.
"""
from __future__ import annotations


from pydantic import BaseModel

from app.evals.runner import run_eval as run_project_eval
from app.evals.store import load_eval_config
from app.models.records.eval_run import EvalRun
from app.tools.shared import validate_project_exists
from app.tools.types import ToolProse


# The stored run whole, so what the caller reports and what the page shows are one
# record: `status`, `metrics`, `notes`, and the stages the run executed.
class EvalRunResult(BaseModel):
    run: EvalRun
    run_url: str


def run_eval(
    project_id: str,
    eval_id: str,
    version_id: str | None = None,
    *,
    base_url: str = "",
) -> EvalRunResult:
    """`base_url` is for a caller whose reader clicks the link; without it it is root-relative."""
    validate_project_exists(project_id)
    run = run_project_eval(
        project_id, load_eval_config(project_id, eval_id),
        version_id=version_id,
    )
    return EvalRunResult(
        run=run,
        run_url=f"{base_url}/project/{project_id}/evals/{eval_id}/runs/{run.run_id}",
    )


# The tour binds this to its own wrapper, which stamps the reader's base_url onto
# `run_url`, so the record carries the prose and no body.
RUN_EVAL = ToolProse(
    description="""\
Score one of the project's evals: it replays the workflow over rows whose right answers
were written down first. Blocks, and a model stage costs real money. Returns the run and
`run_url` — each expected value beside what the workflow produced, the only place the
reader sees WHICH rows disagreed. Hand it over. No pass mark exists: report the metrics
and the disagreements, never a verdict.""",
    parameters={
        "project_id": "The project's name.",
        "eval_id": "The eval's id, as the project's evals page lists it.",
        "version_id": "Which stored version to score, published or not. Omit for the "
            "newest stored.",
    },
)
