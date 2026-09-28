from __future__ import annotations

import json
from pathlib import Path

from critic.corpus import CorpusIndex, InlineComment
from critic.records import CriticRecord, ForeignRecord
from critic.themes import Theme, find_themes_outside, load_theme_vocabulary

HUMAN_AUTHOR = "human"
INLINE_KIND = "inline"
ASKING_SPEECH_ACTS = frozenset({"correction", "instruction", "question"})


class LabeledComment(ForeignRecord):
    html_url: str
    pr: int
    kind: str
    # "human" is the reviewer's own words; "agent" is a coding agent posting under their login.
    author: str
    body: str
    themes: list[str]
    speech_act: str | None
    severity: str | None
    rule: str | None


class _SplitSide(ForeignRecord):
    prs: list[int]
    prs_with_human_comments: list[int]


class _Split(ForeignRecord):
    test: _SplitSide


class LabelSet(CriticRecord):
    labels_path: str
    comments: list[LabeledComment]
    themes: list[Theme]


class ReviewerComment(CriticRecord):
    html_url: str
    pr: int
    thread_id: int
    path: str
    line: int
    start_line: int | None
    commit_sha: str
    created_at: str
    diff_hunk: str
    body: str
    themes: list[str]
    speech_act: str
    severity: str | None
    rule: str | None


class ExcludedLabel(CriticRecord):
    html_url: str
    reason: str


class GroundTruth(CriticRecord):
    comments: list[ReviewerComment]
    excluded: list[ExcludedLabel]


def load_label_set(labels_path: Path, themes_path: Path) -> LabelSet:
    if not labels_path.is_file():
        raise FileNotFoundError(f"labeled-comment file not found: {labels_path}")
    lines = labels_path.read_text(encoding="utf-8").splitlines()
    comments = [LabeledComment.model_validate(json.loads(line)) for line in lines if line.strip()]
    return LabelSet(
        labels_path=str(labels_path), comments=comments, themes=load_theme_vocabulary(themes_path)
    )


def load_test_split(split_path: Path) -> list[int]:
    return sorted(_read_split(split_path).test.prs)


def load_silent_test_prs(split_path: Path) -> list[int]:
    test = _read_split(split_path).test
    return sorted(set(test.prs) - set(test.prs_with_human_comments))


def group_labels_by_pr(label_set: LabelSet) -> dict[int, list[LabeledComment]]:
    labels_by_pr: dict[int, list[LabeledComment]] = {}
    for comment in label_set.comments:
        labels_by_pr.setdefault(comment.pr, []).append(comment)
    return labels_by_pr


def select_reviewer_comments(
    labeled: list[LabeledComment], corpus: CorpusIndex, themes: list[Theme]
) -> GroundTruth:
    outcomes = [_admit_label(label, corpus) for label in labeled]
    admitted = [outcome for outcome in outcomes if isinstance(outcome, ReviewerComment)]
    strays = find_themes_outside((slug for comment in admitted for slug in comment.themes), themes)
    if strays:
        raise ValueError(f"reviewer comments carry themes the vocabulary lacks: {strays}")
    openers = find_thread_openers(admitted)
    return GroundTruth(
        comments=openers,
        excluded=[outcome for outcome in outcomes if isinstance(outcome, ExcludedLabel)]
        + [_exclude_follow_up(comment) for comment in admitted if comment not in openers],
    )


def find_thread_openers(comments: list[ReviewerComment]) -> list[ReviewerComment]:
    """The reviewer's first ask in each thread; a later one answers the agent, not the diff."""
    openers: dict[int, ReviewerComment] = {}
    for comment in sorted(comments, key=lambda comment: comment.created_at):
        openers.setdefault(comment.thread_id, comment)
    return list(openers.values())


def _read_split(split_path: Path) -> _Split:
    return _Split.model_validate_json(split_path.read_text(encoding="utf-8"))


def _admit_label(label: LabeledComment, corpus: CorpusIndex) -> ReviewerComment | ExcludedLabel:
    speech_act = label.speech_act
    raw = corpus.by_url.get(label.html_url)
    if label.kind != INLINE_KIND:
        return _exclude(label.html_url, f"a {label.kind} comment, not an inline one")
    if label.author != HUMAN_AUTHOR:
        return _exclude(label.html_url, f"written by the {label.author}, not the reviewer")
    if speech_act is None or speech_act not in ASKING_SPEECH_ACTS:
        return _exclude(label.html_url, f"speech act {speech_act}: asks for nothing")
    if raw is None:
        return _exclude(label.html_url, "absent from the review-comment corpus")
    if raw.original_line is None:
        return _exclude(label.html_url, "anchored to no line")
    return _build_reviewer_comment(label, raw, raw.original_line, speech_act)


def _exclude(html_url: str, reason: str) -> ExcludedLabel:
    return ExcludedLabel(html_url=html_url, reason=reason)


def _exclude_follow_up(comment: ReviewerComment) -> ExcludedLabel:
    return _exclude(comment.html_url, "follows the reviewer's earlier ask in the same thread")


def _build_reviewer_comment(
    label: LabeledComment, raw: InlineComment, line: int, speech_act: str
) -> ReviewerComment:
    return ReviewerComment(
        html_url=label.html_url,
        pr=label.pr,
        # GitHub points every reply at the thread's first comment, not at the one above it.
        thread_id=raw.id if raw.in_reply_to_id is None else raw.in_reply_to_id,
        path=raw.path,
        line=line,
        start_line=raw.original_start_line,
        commit_sha=raw.original_commit_id,
        created_at=raw.created_at,
        diff_hunk=raw.diff_hunk,
        body=raw.body,
        themes=label.themes,
        speech_act=speech_act,
        severity=label.severity,
        rule=label.rule,
    )
