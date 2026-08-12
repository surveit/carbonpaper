"""The seam a caller that cannot import app.evals runs one of a project's evals through.

app.evals imports app.services, and an import-linter contract holds that arrow one way:
app.web alone may import app.evals. So app.web hands the runner in as it loads, and
app.tools calls run_project_eval here.
"""
from __future__ import annotations

from typing import Protocol

from app.models import EvalRun


class RunOneProjectEval(Protocol):
    def __call__(
        self, project: str, eval_id: str, *, version_id: str | None
    ) -> EvalRun: ...


_run_one_project_eval: RunOneProjectEval | None = None


def configure_eval_runner(runner: RunOneProjectEval) -> None:
    global _run_one_project_eval
    _run_one_project_eval = runner


def run_project_eval(
    project: str, eval_id: str, *, version_id: str | None = None
) -> EvalRun:
    if _run_one_project_eval is None:
        raise RuntimeError(
            "no eval runner is configured in this process, so no eval can be run — "
            "importing app.web.routers.evals is what configures it"
        )
    return _run_one_project_eval(project, eval_id, version_id=version_id)
