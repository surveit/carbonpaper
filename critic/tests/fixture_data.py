from __future__ import annotations

import json
from pathlib import Path

from critic.corpus import CorpusIndex, load_corpus_index
from critic.diff import ChangedFile, PullRequestDiff
from critic.labels import LabeledRange, ReviewerComment, load_labeled_ranges, select_reviewer_comments
from critic.predictions import PredictedComment, Severity

FIXTURES = Path(__file__).with_name("fixtures")
RUBRICS = Path(__file__).resolve().parents[1] / "rubrics"
REPO = "surveit/carbonpaper"
PR = 1045
FIRST_REVIEWED_COMMIT = "a3e285612fc8b68f71124a78f793e54c1b688ca0"
COMPARE_FILE = FIXTURES / "compare-1045-a3e28561.json"


def load_corpus() -> CorpusIndex:
    return load_corpus_index(FIXTURES / "corpus")


def load_range() -> LabeledRange:
    [labeled_range] = load_labeled_ranges(FIXTURES / "labeled")
    return labeled_range


def load_reals_at_first_commit() -> list[ReviewerComment]:
    truth = select_reviewer_comments(load_range().comments, load_corpus())
    return [comment for comment in truth.comments if comment.commit_sha == FIRST_REVIEWED_COMMIT]


def load_diff_at_first_commit() -> PullRequestDiff:
    comparison = json.loads(COMPARE_FILE.read_text(encoding="utf-8"))
    return PullRequestDiff(
        repo=REPO,
        number=PR,
        title=comparison["title"],
        body=comparison["body"],
        base_sha=comparison["base_sha"],
        commit_sha=FIRST_REVIEWED_COMMIT,
        files=[ChangedFile.model_validate(item) for item in comparison["files"]],
    )


def read_fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def echo_as_prediction(
    real: ReviewerComment, line_shift: int = 0, theme: str | None = None, severity: Severity = "should"
) -> PredictedComment:
    """The reviewer's own comment posed as a prediction, shifted or re-themed by the test."""
    if real.rule is None:
        raise ValueError(f"{real.html_url} carries no rule to echo")
    return PredictedComment(
        path=real.path,
        line=real.line + line_shift,
        theme=real.themes[0] if theme is None else theme,
        rule=real.rule,
        text=real.body,
        severity=severity,
    )
