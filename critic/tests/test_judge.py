from __future__ import annotations

import pytest

from critic.backend import BackendReplyError, ModelRequest
from critic.judge import (
    OUTSIDE_THE_DIFF,
    JudgeVerdict,
    build_judge_request,
    validate_one_verdict_per_pair,
    read_prediction_side,
    read_real_side,
)
from critic.labels import ReviewerComment
from critic.matching import pair_same_path
from critic.tests.fixture_data import (
    FIRST_REVIEWED_COMMIT,
    echo_as_prediction,
    load_diff_at_first_commit,
    load_reals,
    load_reals_at_first_commit,
)
from critic.worked_examples import load_worked_examples


def _real_at(line: int) -> ReviewerComment:
    return next(real for real in load_reals_at_first_commit() if real.line == line)


def _build_request_for(real: ReviewerComment) -> ModelRequest:
    predictions = [echo_as_prediction(real, theme="naming")]
    candidates = pair_same_path(predictions, [real], taken=[])
    examples = load_worked_examples().judge
    return build_judge_request(candidates, predictions, [real], load_diff_at_first_commit(), examples)


def test_the_judge_is_told_its_place_and_what_it_cannot_see() -> None:
    request = _build_request_for(_real_at(87))
    assert "YOUR PLACE" in request.system and "WHAT YOU ARE SHOWN, AND WHAT NOT" in request.system
    assert "within 5 lines of one commit" in request.system
    assert "pair_id 1, file app/models/tool_schema_prompts.py" in request.user


def test_each_side_carries_the_code_it_sits_under() -> None:
    real = _real_at(87)
    side = read_real_side(real, FIRST_REVIEWED_COMMIT)
    anchored_line = real.diff_hunk.splitlines()[-1][1:]
    assert side.code.splitlines()[-1].endswith(anchored_line)
    assert side.code.splitlines()[-1].lstrip().startswith("87 ")
    prediction = read_prediction_side(echo_as_prediction(real), load_diff_at_first_commit())
    assert prediction.code == side.code


def test_a_prediction_off_the_diff_says_so() -> None:
    real = _real_at(87)
    off_diff = echo_as_prediction(real).model_copy(update={"path": "app/not_in_this_pr.py"})
    assert read_prediction_side(off_diff, load_diff_at_first_commit()).code == OUTSIDE_THE_DIFF


def test_a_reviewer_comment_from_another_commit_is_marked() -> None:
    later = next(real for real in load_reals() if real.commit_sha != FIRST_REVIEWED_COMMIT)
    assert read_real_side(later, FIRST_REVIEWED_COMMIT).author == "reviewer, on another commit"


def test_the_judges_worked_examples_are_real_comment_pairs() -> None:
    request = _build_request_for(_real_at(87))
    for example in load_worked_examples().judge:
        assert example.reviewer.html_url in request.system and example.other.html_url in request.system
    assert '"same_point":true' in request.system and '"same_point":false' in request.system
    assert "+class StoredFileProfile(BaseModel):" in request.system


def test_a_verdict_for_every_pair_asked_and_no_other() -> None:
    real = _real_at(87)
    candidates = pair_same_path([echo_as_prediction(real)], [real], taken=[])
    validate_one_verdict_per_pair(candidates, [JudgeVerdict(pair_id=1, same_point=False, reason="Apart.")])
    with pytest.raises(BackendReplyError, match="answered pairs"):
        validate_one_verdict_per_pair(candidates, [])
