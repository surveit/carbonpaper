from __future__ import annotations

from critic.labels import ReviewerComment
from critic.matching import (
    LINE_WINDOW,
    accept_greedily,
    assemble_matching,
    match_exactly,
    measure_line_gap,
    pair_same_path,
)
from critic.tests.fixture_data import echo_as_prediction, load_reals_at_first_commit


def _real_at(line: int) -> ReviewerComment:
    return next(real for real in load_reals_at_first_commit() if real.line == line)


def test_the_reviewers_own_comment_matches_itself_exactly() -> None:
    reals = load_reals_at_first_commit()
    pairs = match_exactly([echo_as_prediction(real) for real in reals], reals)
    assert [(pair.prediction_index, pair.real_index, pair.line_gap) for pair in pairs] == [
        (index, index, 0) for index in range(len(reals))
    ]


def test_the_line_window_is_inclusive() -> None:
    real = _real_at(87)
    assert match_exactly([echo_as_prediction(real, LINE_WINDOW)], [real])
    assert not match_exactly([echo_as_prediction(real, LINE_WINDOW + 1)], [real])


def test_a_theme_the_real_comment_lacks_blocks_an_exact_match() -> None:
    real = _real_at(87)
    assert real.themes == ["verbose_prose"]
    assert not match_exactly([echo_as_prediction(real, theme="naming")], [real])


def test_a_prediction_takes_the_nearest_real_comment_first() -> None:
    near, far = _real_at(181), _real_at(186)
    prediction = echo_as_prediction(near, theme="verbose_prose", line_shift=1)
    assert "verbose_prose" in far.themes
    pairs = match_exactly([prediction], [far, near])
    assert [(pair.real_index, pair.line_gap) for pair in pairs] == [(1, 1)]


def test_same_path_pairs_left_by_the_exact_rule_go_to_the_judge() -> None:
    reals = [_real_at(87), _real_at(181)]
    predictions = [echo_as_prediction(reals[0], theme="naming"), echo_as_prediction(reals[1])]
    exact = match_exactly(predictions, reals)
    candidates = pair_same_path(predictions, reals, taken=exact)
    assert [(c.prediction_index, c.real_index) for c in candidates] == [(0, 0)]


def test_a_judged_pair_is_accepted_only_with_a_same_point_reason() -> None:
    reals = [_real_at(87)]
    predictions = [echo_as_prediction(reals[0], theme="naming")]
    candidates = pair_same_path(predictions, reals, taken=[])
    accepted = accept_greedily(candidates, route="judge", reasons={1: "Both ask to cut the prose."}, taken=[])
    matching = assemble_matching(accepted, len(predictions), len(reals))
    assert [(pair.route, pair.judge_reason) for pair in matching.pairs] == [("judge", "Both ask to cut the prose.")]
    assert matching.unmatched_predictions == [] and matching.unmatched_reals == []


def test_the_gap_to_a_multi_line_comment_is_measured_to_its_span() -> None:
    real = _real_at(87).model_copy(update={"start_line": 80})
    assert measure_line_gap(83, real) == 0
    assert measure_line_gap(78, real) == 2
    assert measure_line_gap(90, real) == 3
