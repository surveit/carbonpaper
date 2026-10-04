from __future__ import annotations

import json
from pathlib import Path

from critic.corpus import CorpusIndex, load_corpus_index
from critic.diff import ChangedFile, PullRequestDiff
from critic.labels import LABEL_DIR, LabelSet, ReviewerComment, load_label_set, select_reviewer_comments
from critic.predictions import PredictedComment, Severity

FIXTURES = Path(__file__).with_name("fixtures")
RUBRICS = Path(__file__).resolve().parents[1] / "rubrics"
# The repo's own AGENTS.md stands in for the owner's CLAUDE.md, which the repo never holds.
STAND_IN_ENVIRON = {"CRITIC_OWNER_CLAUDE_MD": str(Path(__file__).resolve().parents[2] / "AGENTS.md")}
REPO = "surveit/carbonpaper"
PR = 1045
FIRST_REVIEWED_COMMIT = "a3e285612fc8b68f71124a78f793e54c1b688ca0"
COMPARE_FILE = FIXTURES / "compare-1045-a3e28561.json"


def load_corpus() -> CorpusIndex:
    return load_corpus_index(FIXTURES / "corpus")


def load_labels() -> LabelSet:
    return load_label_set(FIXTURES / "labels-1045.jsonl", LABEL_DIR / "taxonomy.json")


def load_reals() -> list[ReviewerComment]:
    labels = load_labels()
    return select_reviewer_comments(labels.comments, load_corpus(), labels.themes).comments


def load_reals_at_first_commit() -> list[ReviewerComment]:
    return [comment for comment in load_reals() if comment.commit_sha == FIRST_REVIEWED_COMMIT]


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
