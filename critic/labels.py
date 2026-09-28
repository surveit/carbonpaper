from __future__ import annotations

import json
from pathlib import Path

from pydantic import AliasChoices, Field

from critic.corpus import CorpusIndex, InlineComment
from critic.records import CriticRecord, ForeignRecord
from critic.themes import Theme, load_theme_vocabulary

LABELED_FILE_NAME = "comments.labeled.jsonl"
THEMES_FILE_NAME = "themes.json"
HUMAN_VOICES = frozenset({"human", "user"})
INLINE_KIND = "inline"
ASKING_SPEECH_ACTS = frozenset({"correction", "instruction", "question"})


class LabeledComment(ForeignRecord):
    html_url: str
    pr: int
    kind: str
    body: str
    themes: list[str]
    speech_act: str | None
    severity: str | None
    rule: str | None
    # Each labeler named the field that tells the reviewer's own words from the agent's differently.
    voice: str = Field(validation_alias=AliasChoices("author", "author_inferred", "voice"))


class LabeledRange(CriticRecord):
    directory: str
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


class RangeIndex(CriticRecord):
    by_pr: dict[int, LabeledRange]
    conflicts: dict[int, list[str]]


def load_labeled_ranges(root: Path) -> list[LabeledRange]:
    if not root.is_dir():
        raise FileNotFoundError(f"labeled-comment root not found: {root}")
    return [_load_range(path.parent) for path in sorted(root.glob(f"*/{LABELED_FILE_NAME}"))]


def index_ranges_by_pr(ranges: list[LabeledRange]) -> RangeIndex:
    ranges_by_pr: dict[int, list[LabeledRange]] = {}
    for labeled_range in ranges:
        for pr in sorted({comment.pr for comment in labeled_range.comments}):
            ranges_by_pr.setdefault(pr, []).append(labeled_range)
    return RangeIndex(
        by_pr={pr: found[0] for pr, found in ranges_by_pr.items() if len(found) == 1},
        conflicts={
            pr: [found_range.directory for found_range in found]
            for pr, found in ranges_by_pr.items()
            if len(found) > 1
        },
    )


def select_reviewer_comments(labeled: list[LabeledComment], corpus: CorpusIndex) -> GroundTruth:
    outcomes = [_admit_label(label, corpus) for label in labeled]
    admitted = [outcome for outcome in outcomes if isinstance(outcome, ReviewerComment)]
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


def _admit_label(label: LabeledComment, corpus: CorpusIndex) -> ReviewerComment | ExcludedLabel:
    speech_act = label.speech_act
    raw = corpus.by_url.get(label.html_url)
    if label.kind != INLINE_KIND:
        return _exclude(label.html_url, f"a {label.kind} comment, not an inline one")
    if label.voice not in HUMAN_VOICES:
        return _exclude(label.html_url, f"voice labeled {label.voice}, not the reviewer's own")
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
        body=raw.body,
        themes=label.themes,
        speech_act=speech_act,
        severity=label.severity,
        rule=label.rule,
    )


def _load_range(directory: Path) -> LabeledRange:
    themes_path = directory / THEMES_FILE_NAME
    if not themes_path.is_file():
        raise FileNotFoundError(f"{directory} has labels but no {THEMES_FILE_NAME}")
    lines = (directory / LABELED_FILE_NAME).read_text(encoding="utf-8").splitlines()
    comments = [LabeledComment.model_validate(json.loads(line)) for line in lines if line.strip()]
    return LabeledRange(
        directory=str(directory), comments=comments, themes=load_theme_vocabulary(themes_path)
    )
