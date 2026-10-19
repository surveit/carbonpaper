from __future__ import annotations

from collections.abc import Collection

from critic.labels import ReviewerComment
from critic.matching import Matching, MatchRoute
from critic.predictions import PredictedComment
from critic.records import CriticRecord

ALL_ROUTES: frozenset[MatchRoute] = frozenset({"exact", "judge"})
EXACT_ROUTE: frozenset[MatchRoute] = frozenset({"exact"})


class Score(CriticRecord):
    predicted: int
    real: int
    matched_predictions: int
    matched_reals: int
    precision: float | None
    recall: float | None
    f1: float | None


def build_score(predicted: int, real: int, matched_predictions: int, matched_reals: int) -> Score:
    precision = _divide(matched_predictions, predicted)
    recall = _divide(matched_reals, real)
    return Score(
        predicted=predicted,
        real=real,
        matched_predictions=matched_predictions,
        matched_reals=matched_reals,
        precision=precision,
        recall=recall,
        f1=_compute_harmonic_mean(precision, recall),
    )


def score_unit(
    predictions: list[PredictedComment],
    reals: list[ReviewerComment],
    matching: Matching,
    routes: Collection[MatchRoute],
) -> Score:
    matched = sum(1 for pair in matching.pairs if pair.route in routes)
    return build_score(len(predictions), len(reals), matched, matched)


def score_theme_in_unit(
    predictions: list[PredictedComment],
    reals: list[ReviewerComment],
    matching: Matching,
    theme: str,
) -> Score:
    matched_predictions = {pair.prediction_index for pair in matching.pairs}
    matched_reals = {pair.real_index for pair in matching.pairs}
    themed_predictions = [i for i, prediction in enumerate(predictions) if prediction.theme == theme]
    themed_reals = [i for i, real in enumerate(reals) if theme in real.themes]
    return build_score(
        predicted=len(themed_predictions),
        real=len(themed_reals),
        matched_predictions=len(matched_predictions.intersection(themed_predictions)),
        matched_reals=len(matched_reals.intersection(themed_reals)),
    )


def sum_scores(scores: list[Score]) -> Score:
    return build_score(
        predicted=sum(score.predicted for score in scores),
        real=sum(score.real for score in scores),
        matched_predictions=sum(score.matched_predictions for score in scores),
        matched_reals=sum(score.matched_reals for score in scores),
    )


def _divide(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _compute_harmonic_mean(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)
