from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from critic.labels import group_labels_by_pr, load_label_set, select_reviewer_comments
from critic.tests.fixture_data import FIXTURES, PR, load_corpus, load_labels, load_reals

FOLLOW_UP = "follows the reviewer's earlier ask in the same thread"


def _discussion_id(html_url: str) -> str:
    return html_url.rsplit("#", 1)[1].removeprefix("discussion_r")


def test_the_reviewers_first_ask_in_each_thread_is_kept() -> None:
    assert {_discussion_id(comment.html_url) for comment in load_reals()} == {
        "4036450495", "4036482834", "4036486987", "4036497245", "4036508904",
        "4036515475", "4036520248", "4036526756",
        # First human asks in threads the agent opened with a decision flag.
        "4036463326", "4037402565",
    }


def test_every_other_label_is_excluded_for_a_stated_reason() -> None:
    labels = load_labels()
    truth = select_reviewer_comments(labels.comments, load_corpus(), labels.themes)
    assert Counter(label.reason for label in truth.excluded) == {
        "written by the agent, not the reviewer": 24,
        "a conversation comment, not an inline one": 2,
        "speech act praise: asks for nothing": 1,
        FOLLOW_UP: 3,
    }


def test_later_asks_in_a_thread_are_follow_ups_whoever_opened_it() -> None:
    labels = load_labels()
    truth = select_reviewer_comments(labels.comments, load_corpus(), labels.themes)
    reasons = {_discussion_id(label.html_url): label.reason for label in truth.excluded}
    assert reasons["4036483742"] == FOLLOW_UP  # the reviewer opened this thread
    assert reasons["4037391701"] == FOLLOW_UP  # the agent opened this one
    assert reasons["4037395943"] == FOLLOW_UP


def test_a_reply_carries_its_threads_first_comment_as_thread_id() -> None:
    reply = next(c for c in load_reals() if _discussion_id(c.html_url) == "4036463326")
    assert reply.thread_id == 4035544602


def test_a_reviewer_comment_carries_the_line_and_commit_it_was_left_on() -> None:
    comment = next(c for c in load_reals() if _discussion_id(c.html_url) == "4036486987")
    assert (comment.path, comment.line, comment.commit_sha[:8]) == (
        "app/models/tool_schema_prompts.py", 87, "a3e28561",
    )
    assert comment.body.startswith("I think overall could trim this by like 40%")
    assert comment.themes == ["verbose_prose"]


def test_labels_group_by_pr() -> None:
    assert list(group_labels_by_pr(load_labels())) == [PR]


def test_a_theme_the_vocabulary_lacks_is_refused(tmp_path: Path) -> None:
    taxonomy = json.loads((FIXTURES / "taxonomy-themes.json").read_text(encoding="utf-8"))
    del taxonomy["themes"]["verbose_prose"]
    themes_path = tmp_path / "taxonomy.json"
    themes_path.write_text(json.dumps(taxonomy), encoding="utf-8")
    labels = load_label_set(FIXTURES / "labels-1045.jsonl", themes_path)
    with pytest.raises(ValueError, match="verbose_prose"):
        select_reviewer_comments(labels.comments, load_corpus(), labels.themes)


def test_a_missing_label_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="labeled-comment file not found"):
        load_label_set(tmp_path / "corpus.human.jsonl", FIXTURES / "taxonomy-themes.json")
