"""Running one of a project's evals, as a tool: the score, and the page it is read on.

app.tools cannot import app.evals, so the run goes through app.services.eval_run.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel

from app.core.agent.tool_spec import ToolSpec
from app.models import EvalRun
from app.services.eval_run import run_project_eval
from app.tools.shared import resolve_existing_project
from app.tools.types import ToolInputSchema


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
    resolve_existing_project(project_id)
    run = run_project_eval(project_id, eval_id, version_id=version_id)
    return EvalRunResult(
        run=run,
        run_url=f"{base_url}/project/{project_id}/evals/{eval_id}/runs/{run.id}",
    )


RUN_EVAL = ToolSpec(
    name="run_eval",
    description="""\
Run one of the project's evals now and return what it scored. An eval replays the
workflow over rows whose right answers were written down first: it injects that
dataset at the eval's override stage, executes only the stages from there to its
target stage, and compares the target's output against the expected column. Nothing
upstream runs again, and no production run is recorded.

This BLOCKS until the score is in — there is no run id to poll, unlike run_workflow —
and an eval whose target is a model stage spends real calls and real minutes doing it.

Returns the stored run plus `run_url`: the page laying every expected value beside
what the workflow actually produced, row by row. Hand that URL to the reader. It is
the evidence for anything you say about the score, and the only place they can see
WHICH rows disagreed.

`run.status` says what happened, never whether it is good:
  scored — the comparison ran. `run.metrics` carries rows_scored, rows_passed,
           accuracy, and an accuracy per compared column.
  vetoed — the path from override to target does not hold one row in, one row out,
           so it cannot be scored row by row; `run.notes` names the stages.
  error  — a stage failed; `run.notes` carries the failure. No score is invented.

There is no pass mark, so do not announce one. Report the metrics, then read the
disagreeing rows and say what the workflow answered instead — whether that is good
enough is the reader's judgment, and it is the disagreements they learn from.""",
)

RUN_EVAL_SCHEMA: ToolInputSchema = {
    "project_id": Annotated[str, "The project's name."],
    "eval_id": Annotated[str, "The eval's id, as the project's evals page lists it."],
    "version_id": Annotated[
        str | None,
        "Which stored version to score, published or not. Omit for the newest stored.",
    ],
}
