"""Whether an eval vouches for one stage, for the stage panel's badge. Coverage attaches to
the eval's TARGET stage alone — the other stages on the pathway executed, but nothing compared
what they produced to anything.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.models import EvalConfig, EvalRun
from app.evals.store import list_project_eval_configs, list_project_eval_runs
from app.services.workspace import resolve_project_dir
from app.web.eval_run_view import tally_scored_rows

CoverageStatus = Literal["checked", "mismatches", "stale"]


class EvalCoverage(BaseModel):
    """`checked` means every scored row matched — not that the step is right."""

    status: CoverageStatus
    eval_name: str
    href: str
    columns: list[str]
    rows_total: int
    rows_passed: int
    # The version the eval run scored. Named on every state, because that is what the
    # verdict is about; on `stale` it is the whole point.
    scored_version: str


def find_eval_coverage(
    project: str, stage_id: str, version_id: str | None
) -> EvalCoverage | None:
    """`version_id` is the version the reader is looking at; None means none resolved."""
    for config in _find_evals_targeting(project, stage_id):
        run = _latest_scored_run(project, config.id)
        if run is None:
            continue
        coverage = _build_coverage(project, config, run, version_id)
        if coverage is not None:
            return coverage
    return None


def _find_evals_targeting(project: str, stage_id: str) -> list[EvalConfig]:
    return [
        entry.config
        for entry in list_project_eval_configs(project)
        if entry.config is not None and entry.config.target_stage == stage_id
    ]


def _latest_scored_run(project: str, config_id: str) -> EvalRun | None:
    try:
        runs = list_project_eval_runs(project, config_id)
    except (OSError, ValueError):
        # One unreadable run record must not put a badge on the page or take it down.
        return None
    return next((run for run in runs if run.status == "scored" and run.result_ref), None)


def _build_coverage(
    project: str, config: EvalConfig, run: EvalRun, version_id: str | None
) -> EvalCoverage | None:
    assert run.result_ref is not None  # _latest_scored_run required one
    # None where the result table will not read: the badge is then absent, never guessed.
    # resolve_project_dir, not a joined path — it refuses an id escaping the workspace.
    tally = tally_scored_rows(resolve_project_dir(project) / run.result_ref)
    if tally is None:
        return None
    return EvalCoverage(
        status=_judge(run, version_id, tally.passed, tally.total),
        eval_name=config.name,
        href=f"/project/{project}/evals/{config.id}/runs/{run.id}",
        columns=tally.columns,
        rows_total=tally.total,
        rows_passed=tally.passed,
        scored_version=run.workflow_version,
    )


def _judge(run: EvalRun, version_id: str | None, passed: int, total: int) -> CoverageStatus:
    # Staleness outranks the score: a verdict on other code is not one on this code.
    if version_id is None or run.workflow_version != version_id:
        return "stale"
    return "checked" if passed == total else "mismatches"
