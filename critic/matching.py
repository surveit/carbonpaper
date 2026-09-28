from __future__ import annotations

from typing import Literal

from critic.labels import ReviewerComment
from critic.predictions import PredictedComment
from critic.records import CriticRecord

LINE_WINDOW = 5
MatchRoute = Literal["exact", "judge"]


class CandidatePair(CriticRecord):
    pair_id: int
    prediction_index: int
    real_index: int
    line_gap: int


class MatchedPair(CriticRecord):
    prediction_index: int
    real_index: int
    line_gap: int
    route: MatchRoute
    judge_reason: str | None


class Matching(CriticRecord):
    pairs: list[MatchedPair]
    unmatched_predictions: list[int]
    unmatched_reals: list[int]


def measure_line_gap(line: int, real: ReviewerComment) -> int:
    first_line = real.line if real.start_line is None else real.start_line
    if first_line <= line <= real.line:
        return 0
    return min(abs(line - first_line), abs(line - real.line))


def match_exactly(
    predictions: list[PredictedComment], reals: list[ReviewerComment]
) -> list[MatchedPair]:
    candidates = [
        candidate
        for candidate in pair_same_path(predictions, reals, taken=[])
        if candidate.line_gap <= LINE_WINDOW
        and predictions[candidate.prediction_index].theme in reals[candidate.real_index].themes
    ]
    return accept_greedily(candidates, route="exact", reasons={}, taken=[])


def pair_same_path(
    predictions: list[PredictedComment], reals: list[ReviewerComment], taken: list[MatchedPair]
) -> list[CandidatePair]:
    used_predictions = {pair.prediction_index for pair in taken}
    used_reals = {pair.real_index for pair in taken}
    index_pairs = [
        (prediction_index, real_index)
        for prediction_index, prediction in enumerate(predictions)
        for real_index, real in enumerate(reals)
        if prediction.path == real.path
        and prediction_index not in used_predictions
        and real_index not in used_reals
    ]
    return [
        CandidatePair(
            pair_id=pair_id,
            prediction_index=prediction_index,
            real_index=real_index,
            line_gap=measure_line_gap(predictions[prediction_index].line, reals[real_index]),
        )
        for pair_id, (prediction_index, real_index) in enumerate(index_pairs, 1)
    ]


def accept_greedily(
    candidates: list[CandidatePair],
    route: MatchRoute,
    reasons: dict[int, str],
    taken: list[MatchedPair],
) -> list[MatchedPair]:
    """Nearest lines first, so one prediction never claims a real comment a closer one fits."""
    used_predictions = {pair.prediction_index for pair in taken}
    used_reals = {pair.real_index for pair in taken}
    accepted: list[MatchedPair] = []
    for candidate in sorted(candidates, key=_rank_candidate):
        if candidate.prediction_index in used_predictions or candidate.real_index in used_reals:
            continue
        used_predictions.add(candidate.prediction_index)
        used_reals.add(candidate.real_index)
        accepted.append(_build_pair(candidate, route, reasons.get(candidate.pair_id)))
    return accepted


def assemble_matching(
    pairs: list[MatchedPair], prediction_count: int, real_count: int
) -> Matching:
    matched_predictions = {pair.prediction_index for pair in pairs}
    matched_reals = {pair.real_index for pair in pairs}
    return Matching(
        pairs=sorted(pairs, key=lambda pair: pair.real_index),
        unmatched_predictions=[i for i in range(prediction_count) if i not in matched_predictions],
        unmatched_reals=[i for i in range(real_count) if i not in matched_reals],
    )


def _rank_candidate(candidate: CandidatePair) -> tuple[int, int, int]:
    return (candidate.line_gap, candidate.prediction_index, candidate.real_index)


def _build_pair(candidate: CandidatePair, route: MatchRoute, reason: str | None) -> MatchedPair:
    return MatchedPair(
        prediction_index=candidate.prediction_index,
        real_index=candidate.real_index,
        line_gap=candidate.line_gap,
        route=route,
        judge_reason=reason,
    )
