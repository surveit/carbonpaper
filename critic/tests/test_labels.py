from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

from critic.labels import (
    HUMAN_VOICES,
    LabeledComment,
    index_ranges_by_pr,
    load_labeled_ranges,
    select_reviewer_comments,
)
from critic.tests.fixture_data import FIXTURES, PR, load_corpus, load_range

FOLLOW_UP = "follows the reviewer's earlier ask in the same thread"


def _discussion_id(html_url: str) -> str:
    return html_url.rsplit("#", 1)[1].removeprefix("discussion_r")


def test_the_reviewers_first_ask_in_each_thread_is_kept() -> None:
    truth = select_reviewer_comments(load_range().comments, load_corpus())
    assert {_discussion_id(comment.html_url) for comment in truth.comments} == {
        "4036450495", "4036482834", "4036486987", "4036497245", "4036508904",
        "4036515475", "4036520248", "4036526756",
        # First human asks in threads the agent opened with a decision flag.
        "4036463326", "4037402565",
    }


def test_every_other_label_is_excluded_for_a_stated_reason() -> None:
    truth = select_reviewer_comments(load_range().comments, load_corpus())
    assert Counter(label.reason for label in truth.excluded) == {
        "voice labeled agent, not the reviewer's own": 24,
        "a conversation comment, not an inline one": 2,
        "speech act praise: asks for nothing": 1,
        FOLLOW_UP: 3,
    }


def test_later_asks_in_a_thread_are_follow_ups_whoever_opened_it() -> None:
    truth = select_reviewer_comments(load_range().comments, load_corpus())
    reasons = {_discussion_id(label.html_url): label.reason for label in truth.excluded}
    assert reasons["4036483742"] == FOLLOW_UP  # the reviewer opened this thread
    assert reasons["4037391701"] == FOLLOW_UP  # the agent opened this one
    assert reasons["4037395943"] == FOLLOW_UP


def test_a_reply_carries_its_threads_first_comment_as_thread_id() -> None:
    truth = select_reviewer_comments(load_range().comments, load_corpus())
    reply = next(c for c in truth.comments if _discussion_id(c.html_url) == "4036463326")
    assert reply.thread_id == 4035544602


def test_a_reviewer_comment_carries_the_line_and_commit_it_was_left_on() -> None:
    truth = select_reviewer_comments(load_range().comments, load_corpus())
    comment = next(c for c in truth.comments if _discussion_id(c.html_url) == "4036486987")
    assert (comment.path, comment.line, comment.commit_sha[:8]) == (
        "app/models/tool_schema_prompts.py", 87, "a3e28561",
    )
    assert comment.body.startswith("I think overall could trim this by like 40%")
    assert comment.themes == ["verbose_prose"]


def test_each_labelers_name_for_the_voice_field_is_read() -> None:
    rows = json.loads((FIXTURES / "labels_other_voices.json").read_text(encoding="utf-8"))
    voices = [LabeledComment.model_validate(row).voice for row in rows]
    assert sorted(voices) == ["agent", "agent", "human", "user"]
    assert {"human", "user"} <= HUMAN_VOICES


def test_a_pr_labeled_in_two_ranges_is_a_conflict_not_a_pick(tmp_path: Path) -> None:
    source = FIXTURES / "labeled" / "range_937_1096"
    shutil.copytree(source, tmp_path / "range_937_1096")
    shutil.copytree(source, tmp_path / "range_937_1096_rerun")
    ranges = index_ranges_by_pr(load_labeled_ranges(tmp_path))
    assert PR not in ranges.by_pr
    assert len(ranges.conflicts[PR]) == 2


def test_a_range_directory_without_labels_is_passed_over(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES / "labeled" / "range_937_1096", tmp_path / "range_937_1096")
    (tmp_path / "range_1_225").mkdir()
    assert [Path(r.directory).name for r in load_labeled_ranges(tmp_path)] == ["range_937_1096"]
