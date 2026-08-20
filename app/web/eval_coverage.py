"""Whether an eval vouches for one stage, for the stage panel's badge. Coverage attaches to
the eval's TARGET stage alone — the other stages on the pathway executed, but nothing compared
what they produced to anything.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.models import EvalConfig, EvalRun
from app.evals.store import list_eval_configs, list_eval_runs, resolve_eval_result_path
from app.web.eval_run_view import count_scored_rows

CoverageStatus = Literal["checked", "mismatches", "stale"]

_SEVERITY = {"mismatches": 0, "stale": 1, "checked": 2}


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


def find_eval_coverages(
    project_id: str, stage_id: str, version_id: str | None
) -> list[EvalCoverage]:
    """`version_id` is the version the reader is looking at; None means none resolved."""
    coverages = []
    for config in _find_evals_targeting(project_id, stage_id):
        run = _latest_scored_run(project_id, config.id)
        if run is None:
            continue
        coverage = _build_coverage(project_id, config, run, version_id)
        if coverage is not None:
            coverages.append(coverage)
    # Worst first: a reader scanning the column meets what needs attention, and the
    # order does not shuffle when an eval is renamed.
    return sorted(coverages, key=lambda c: (_SEVERITY[c.status], c.eval_name))


def _find_evals_targeting(project_id: str, stage_id: str) -> list[EvalConfig]:
    return [
        entry.config
        for entry in list_eval_configs(project_id)
        if entry.config is not None and entry.config.target_stage == stage_id
    ]


def _latest_scored_run(project_id: str, config_id: str) -> EvalRun | None:
    try:
        runs = list_eval_runs(project_id, config_id)
    except (OSError, ValueError):
        # One unreadable run record must not put a badge on the page or take it down.
        return None
    return next((run for run in runs if run.status == "scored" and run.result_ref), None)


def _build_coverage(
    project_id: str, config: EvalConfig, run: EvalRun, version_id: str | None
) -> EvalCoverage | None:
    assert run.result_ref is not None  # _latest_scored_run required one
    # None where the result table will not read: the badge is then absent, never guessed.
    score = count_scored_rows(resolve_eval_result_path(project_id, run.result_ref))
    if score is None:
        return None
    return EvalCoverage(
        status=_judge(run, version_id, score.passed, score.total),
        eval_name=config.name,
        href=f"/project/{project_id}/evals/{config.id}/runs/{run.id}",
        columns=score.columns,
        rows_total=score.total,
        rows_passed=score.passed,
        scored_version=run.workflow_version,
    )


def _judge(run: EvalRun, version_id: str | None, passed: int, total: int) -> CoverageStatus:
    # Staleness outranks the score: a verdict on other code is not one on this code.
    if version_id is None or run.workflow_version != version_id:
        return "stale"
    return "checked" if passed == total else "mismatches"
