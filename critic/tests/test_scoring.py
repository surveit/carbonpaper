from __future__ import annotations

import pytest

from critic.matching import assemble_matching, match_exactly
from critic.scoring import ALL_ROUTES, build_score, score_theme_in_unit, score_unit, sum_scores
from critic.tests.fixture_data import (
    FIRST_REVIEWED_COMMIT,
    echo_as_prediction,
    load_reals_at_first_commit,
)


def test_precision_recall_and_f1_follow_the_counts() -> None:
    score = build_score(predicted=4, real=8, matched_predictions=2, matched_reals=2)
    assert (score.precision, score.recall) == (0.5, 0.25)
    assert score.f1 == pytest.approx(1 / 3)


def test_a_ratio_with_nothing_under_it_is_unknown_not_zero() -> None:
    score = build_score(predicted=0, real=3, matched_predictions=0, matched_reals=0)
    assert (score.precision, score.recall, score.f1) == (None, 0.0, None)


def test_no_match_at_all_scores_an_f1_of_zero() -> None:
    assert build_score(predicted=2, real=2, matched_predictions=0, matched_reals=0).f1 == 0.0


def test_units_add_up_by_their_counts_not_their_ratios() -> None:
    first = build_score(predicted=1, real=1, matched_predictions=1, matched_reals=1)
    second = build_score(predicted=9, real=1, matched_predictions=0, matched_reals=0)
    total = sum_scores([first, second])
    assert (total.predicted, total.real, total.matched_reals) == (10, 2, 1)
    assert (total.precision, total.recall) == (0.1, 0.5)


def test_a_theme_scores_its_predictions_and_the_real_comments_that_carry_it() -> None:
    reals = load_reals_at_first_commit()
    verbose = [real for real in reals if "verbose_prose" in real.themes]
    caught = verbose[0]
    predictions = [echo_as_prediction(caught, theme="verbose_prose")]
    matching = assemble_matching(match_exactly(predictions, reals, FIRST_REVIEWED_COMMIT), len(predictions), len(reals))
    theme_score = score_theme_in_unit(predictions, reals, matching, "verbose_prose")
    assert (theme_score.predicted, theme_score.real, theme_score.matched_reals) == (1, len(verbose), 1)
    overall = score_unit(predictions, reals, matching, ALL_ROUTES)
    assert (overall.predicted, overall.real, overall.matched_reals) == (1, len(reals), 1)
