from __future__ import annotations

import pytest

from critic.backend import ModelRequest
from critic.diff import parse_patch
from critic.prompt import build_review_request
from critic.rubric import NO_RUBRIC_TEXT, load_rubric
from critic.tests.fixture_data import RUBRICS, STAND_IN_ENVIRON, load_diff_at_first_commit, load_labels
from critic.themes import select_flag_themes
from critic.worked_examples import load_worked_examples


def test_the_system_prompt_states_the_critics_place_and_what_it_is_not_told() -> None:
    request = _build_request("none")
    for heading in ("YOUR PLACE", "WHAT YOU ARE SHOWN", "WHAT YOU ARE NOT TOLD", "WHAT BECOMES OF YOUR ANSWER"):
        assert heading in request.system
    assert "The reviewer's comments on this pull request" in request.system
    assert "within 5 lines" in request.system
    assert "gates merges" in request.system


def test_the_worked_examples_are_real_comments_with_their_links() -> None:
    request = _build_request("none")
    for example in load_worked_examples().review:
        assert example.html_url in request.system
        assert example.answer.text in request.system


def test_each_worked_example_sits_on_the_last_line_of_its_diff_hunk() -> None:
    for example in load_worked_examples().review:
        assert parse_patch(example.diff_hunk)[-1].new_line == example.answer.line


def test_the_theme_list_offers_flags_only() -> None:
    request = _build_request("none")
    offered = request.answer_schema["$defs"]["PredictedComment"]["properties"]["theme"]["enum"]
    assert "naming" in offered and "code_placement" in offered
    assert "praise_or_signoff" not in offered
    assert not [slug for slug in offered if slug.startswith("agent_")]
    assert "- naming: A name misdescribes what a value holds" in request.system


def test_an_empty_rubric_says_so() -> None:
    request = _build_request("none")
    assert f"HOUSE RULES: none\n{NO_RUBRIC_TEXT}" in request.system


def test_the_instructions_rubric_carries_each_snapshot_whole() -> None:
    request = _build_request("current_instructions")
    rubric = load_rubric(RUBRICS / "current_instructions", STAND_IN_ENVIRON)
    for file in rubric.files:
        assert f"--- {file.name} ---" in request.system
        assert file.text.strip() in request.system


def test_the_user_prompt_is_the_pull_request_as_the_reviewer_read_it() -> None:
    diff = load_diff_at_first_commit()
    request = _build_request("none")
    assert request.user.startswith(f"Pull request #{diff.number}: {diff.title}\nCommit: {diff.commit_sha}")
    assert f"DIFF ({len(diff.files)} files)" in request.user
    assert "    87 +" in request.user


def test_a_worked_example_outside_the_theme_vocabulary_is_refused() -> None:
    themes = [theme for theme in select_flag_themes(load_labels().themes) if theme.slug != "code_placement"]
    rubric = load_rubric(RUBRICS / "none")
    with pytest.raises(ValueError, match="outside this vocabulary"):
        build_review_request(load_diff_at_first_commit(), rubric, themes, load_worked_examples().review)


def test_without_a_vocabulary_the_theme_is_left_free() -> None:
    rubric = load_rubric(RUBRICS / "none")
    request = build_review_request(load_diff_at_first_commit(), rubric, None, load_worked_examples().review)
    assert "enum" not in request.answer_schema["$defs"]["PredictedComment"]["properties"]["theme"]
    assert "THEMES\nNone supplied." in request.system


def _build_request(rubric_name: str) -> ModelRequest:
    themes = select_flag_themes(load_labels().themes)
    rubric = load_rubric(RUBRICS / rubric_name, STAND_IN_ENVIRON)
    return build_review_request(load_diff_at_first_commit(), rubric, themes, load_worked_examples().review)
