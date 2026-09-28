from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from critic.backend import ModelBackend
from critic.corpus import load_corpus_index
from critic.labels import ExcludedLabel, group_labels_by_pr, load_label_set
from critic.planning import EvalPlan, SkippedPr, plan_silent_units, plan_units
from critic.records import CriticRecord
from critic.rubric import Rubric, RubricStamp, load_rubric, stamp_rubric
from critic.scoring import EXACT_ROUTE, Score, score_theme_in_unit, score_unit, sum_scores
from critic.themes import Theme
from critic.unit_scoring import ScoredUnit, UnitContext, UnscoredUnit, evaluate_unit

DEFAULT_PR_RANGE = (937, 1096)


class EvalSettings(CriticRecord):
    repo: str
    rubric_dir: str
    pr_numbers: list[int]
    # Merged PRs the reviewer left no comment on: every prediction on them is a false one.
    silent_pr_numbers: list[int]
    limit: int | None
    corpus_dir: str
    labels_path: str
    themes_path: str
    diff_dir: str
    review_model: str | None
    judge_model: str | None
    jobs: int


class ThemeScore(CriticRecord):
    theme: str
    definition: str | None
    score: Score


class EvalResult(CriticRecord):
    settings: EvalSettings
    rubric_files: list[RubricStamp]
    started_at: str
    finished_at: str
    scored: list[ScoredUnit]
    unscored: list[UnscoredUnit]
    skipped_prs: list[SkippedPr]
    excluded_labels: list[ExcludedLabel]
    overall: Score
    exact_only: Score
    by_theme: list[ThemeScore]
    review_cost_usd: float
    judge_cost_usd: float
    review_model_ids: list[str]
    judge_model_ids: list[str]


def run_eval(
    settings: EvalSettings, review_backend: ModelBackend, judge_backend: ModelBackend
) -> EvalResult:
    started_at = _stamp_now()
    rubric = load_rubric(Path(settings.rubric_dir))
    corpus = load_corpus_index(Path(settings.corpus_dir))
    label_set = load_label_set(Path(settings.labels_path), Path(settings.themes_path))
    labels_by_pr = group_labels_by_pr(label_set)
    plan = plan_units(settings.repo, settings.pr_numbers, settings.limit, corpus, labels_by_pr, label_set.themes)
    silent = plan_silent_units(settings.repo, settings.silent_pr_numbers, labels_by_pr, label_set.themes)
    context = UnitContext(settings.repo, Path(settings.diff_dir), rubric, review_backend, judge_backend)
    with ThreadPoolExecutor(max_workers=settings.jobs) as pool:
        outcomes = list(pool.map(lambda unit: evaluate_unit(unit, context), plan.units + silent))
    return assemble_result(settings, rubric, plan, outcomes, label_set.themes, started_at)


def assemble_result(
    settings: EvalSettings,
    rubric: Rubric,
    plan: EvalPlan,
    outcomes: list[ScoredUnit | UnscoredUnit],
    vocabulary: list[Theme],
    started_at: str,
) -> EvalResult:
    scored = [outcome for outcome in outcomes if isinstance(outcome, ScoredUnit)]
    return EvalResult(
        settings=settings,
        rubric_files=stamp_rubric(rubric),
        started_at=started_at,
        finished_at=_stamp_now(),
        scored=scored,
        unscored=[outcome for outcome in outcomes if isinstance(outcome, UnscoredUnit)],
        skipped_prs=plan.skipped_prs,
        excluded_labels=plan.excluded_labels,
        overall=sum_scores([unit.score for unit in scored]),
        exact_only=sum_scores([_score_exact(unit) for unit in scored]),
        by_theme=score_themes(scored, vocabulary),
        review_cost_usd=sum(unit.review.cost_usd for unit in scored),
        judge_cost_usd=sum(unit.judge_cost_usd for unit in scored),
        review_model_ids=sorted({model for unit in scored for model in unit.review.model_ids}),
        judge_model_ids=sorted({model for unit in scored for model in unit.judge_model_ids}),
    )


def score_themes(scored: list[ScoredUnit], vocabulary: list[Theme]) -> list[ThemeScore]:
    definitions = {theme.slug: theme.definition for theme in vocabulary}
    used = {prediction.theme for unit in scored for prediction in unit.review.predictions}
    used |= {theme for unit in scored for real in unit.reals for theme in real.themes}
    theme_scores = [
        ThemeScore(
            theme=slug,
            definition=definitions.get(slug),
            score=sum_scores([_score_theme(unit, slug) for unit in scored]),
        )
        for slug in used
    ]
    return sorted(theme_scores, key=lambda item: (-item.score.real, -item.score.predicted, item.theme))


def _score_exact(unit: ScoredUnit) -> Score:
    return score_unit(unit.review.predictions, unit.reals, unit.matching, EXACT_ROUTE)


def _score_theme(unit: ScoredUnit, theme: str) -> Score:
    return score_theme_in_unit(unit.review.predictions, unit.reals, unit.matching, theme)


def _stamp_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
