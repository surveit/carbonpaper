from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from critic.corpus import PullRequestListing
from critic.evaluation import EvalResult
from critic.records import CriticRecord
from critic.report import render_template
from critic.sampling import SIZE_BUCKETS, find_size_bucket
from critic.scoring import Score, build_score, sum_scores
from critic.unit_scoring import ScoredUnit, UnscoredUnit

COMPARISON_JSON = "comparison.json"
COMPARISON_HTML = "comparison.html"


class RunSummary(CriticRecord):
    rubric: str
    results_path: str
    review_model_ids: list[str]
    overall: Score
    exact_only: Score
    on_commented_prs: Score
    silent_predictions: int
    scored_units: int
    unscored_units: int
    review_cost_usd: float
    judge_cost_usd: float


class ThemeComparison(CriticRecord):
    theme: str
    scores: list[Score]


class ScoredCell(CriticRecord):
    status: Literal["scored"]
    predicted: int
    matched_exact: int
    matched_judge: int
    cost_usd: float


class UnscoredCell(CriticRecord):
    status: Literal["skipped", "failed"]
    reason: str


class PrComparison(CriticRecord):
    pr: int
    commit_sha: str
    changed_lines: int
    size_bucket: str
    real: int
    cells: list[ScoredCell | UnscoredCell]


class BucketComparison(CriticRecord):
    size_bucket: str
    silent_prs: int
    scored_prs: list[int]
    predictions: list[int]


class Comparison(CriticRecord):
    repo: str
    notes: list[str]
    runs: list[RunSummary]
    themes: list[ThemeComparison]
    prs: list[PrComparison]
    silent_buckets: list[BucketComparison]


def compare_runs(
    runs: dict[Path, EvalResult], listings: list[PullRequestListing], notes: list[str], out_dir: Path
) -> Comparison:
    results = list(runs.values())
    _require_same_units(runs)
    prs = compare_prs(results, {item.number: item for item in listings})
    return Comparison(
        repo=results[0].settings.repo,
        notes=notes,
        runs=[summarize_run(path, result, out_dir) for path, result in runs.items()],
        themes=compare_themes(results),
        prs=prs,
        silent_buckets=compare_silent_buckets(prs),
    )


def write_comparison_outputs(comparison: Comparison, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / COMPARISON_JSON
    json_path.write_text(comparison.model_dump_json(indent=2), encoding="utf-8")
    html_path = out_dir / COMPARISON_HTML
    html_path.write_text(render_template(COMPARISON_HTML, comparison=comparison), encoding="utf-8")
    return [json_path, html_path]


def read_notes(path: Path) -> list[str]:
    paragraphs = path.read_text(encoding="utf-8").split("\n\n")
    return [" ".join(paragraph.split()) for paragraph in paragraphs if paragraph.strip()]


def summarize_run(path: Path, result: EvalResult, out_dir: Path) -> RunSummary:
    commented = [unit for unit in result.scored if unit.reals]
    silent = [unit for unit in result.scored if not unit.reals]
    return RunSummary(
        rubric=Path(result.settings.rubric_dir).name,
        results_path=os.path.relpath(path, out_dir),
        review_model_ids=result.review_model_ids,
        overall=result.overall,
        exact_only=result.exact_only,
        on_commented_prs=sum_scores([unit.score for unit in commented]),
        silent_predictions=sum(unit.score.predicted for unit in silent),
        scored_units=len(result.scored),
        unscored_units=len(result.unscored),
        review_cost_usd=result.review_cost_usd,
        judge_cost_usd=result.judge_cost_usd,
    )


def compare_themes(results: list[EvalResult]) -> list[ThemeComparison]:
    by_run = [{item.theme: item.score for item in result.by_theme} for result in results]
    empty = build_score(predicted=0, real=0, matched_predictions=0, matched_reals=0)
    slugs = sorted({slug for scores in by_run for slug in scores})
    rows = [ThemeComparison(theme=slug, scores=[scores.get(slug, empty) for scores in by_run]) for slug in slugs]
    return sorted(rows, key=lambda row: (-max(score.real for score in row.scores), row.theme))


def compare_prs(results: list[EvalResult], listings: dict[int, PullRequestListing]) -> list[PrComparison]:
    units_by_run = [{unit.pr: unit for unit in _list_units(result)} for result in results]
    rows = [_compare_pr(pr, [units[pr] for units in units_by_run], listings) for pr in units_by_run[0]]
    return sorted(rows, key=lambda row: (row.real == 0, row.pr))


def compare_silent_buckets(prs: list[PrComparison]) -> list[BucketComparison]:
    silent = [row for row in prs if row.real == 0]
    rows_by_bucket = [[row for row in silent if row.size_bucket == name] for name, _ in SIZE_BUCKETS]
    return [_compare_bucket(rows) for rows in rows_by_bucket if rows]


def _compare_pr(
    pr: int, units: list[ScoredUnit | UnscoredUnit], listings: dict[int, PullRequestListing]
) -> PrComparison:
    if pr not in listings:
        raise ValueError(f"the PR listing lacks PR {pr}, so its size is unknown")
    changed_lines = listings[pr].changed_lines
    return PrComparison(
        pr=pr,
        commit_sha=units[0].commit_sha,
        changed_lines=changed_lines,
        size_bucket=find_size_bucket(changed_lines),
        real=len(units[0].reals),
        cells=[_build_cell(unit) for unit in units],
    )


def _build_cell(unit: ScoredUnit | UnscoredUnit) -> ScoredCell | UnscoredCell:
    if isinstance(unit, UnscoredUnit):
        return UnscoredCell(status=unit.status, reason=unit.reason)
    routes = [pair.route for pair in unit.matching.pairs]
    return ScoredCell(
        status="scored",
        predicted=unit.score.predicted,
        matched_exact=routes.count("exact"),
        matched_judge=routes.count("judge"),
        cost_usd=unit.review.cost_usd + unit.judge_cost_usd,
    )


def _compare_bucket(rows: list[PrComparison]) -> BucketComparison:
    cells_by_run = [[row.cells[run] for row in rows] for run in range(len(rows[0].cells))]
    scored_by_run = [[cell for cell in cells if isinstance(cell, ScoredCell)] for cells in cells_by_run]
    return BucketComparison(
        size_bucket=rows[0].size_bucket,
        silent_prs=len(rows),
        scored_prs=[len(cells) for cells in scored_by_run],
        predictions=[sum(cell.predicted for cell in cells) for cells in scored_by_run],
    )


def _require_same_units(runs: dict[Path, EvalResult]) -> None:
    unit_sets = {
        path: sorted((unit.pr, unit.commit_sha) for unit in _list_units(result)) for path, result in runs.items()
    }
    first = next(iter(unit_sets.values()))
    differing = [str(path) for path, units in unit_sets.items() if units != first]
    if differing:
        raise ValueError(f"runs read different PRs or commits, so they do not compare: {differing}")


def _list_units(result: EvalResult) -> list[ScoredUnit | UnscoredUnit]:
    return [*result.scored, *result.unscored]
