from __future__ import annotations

from pathlib import Path

from critic.predictions import PredictedComment
from critic.records import CriticRecord

WORKED_EXAMPLES_FILE = Path(__file__).with_name("worked_examples.json")


class ReviewExample(CriticRecord):
    html_url: str
    pr: int
    commit_sha: str
    lesson: str
    diff_hunk: str
    answer: PredictedComment


class QuotedComment(CriticRecord):
    html_url: str
    path: str
    line: int
    body: str


class JudgeExample(CriticRecord):
    reviewer: QuotedComment
    other: QuotedComment
    same_point: bool
    reason: str


class WorkedExamples(CriticRecord):
    review: list[ReviewExample]
    judge: list[JudgeExample]


def load_worked_examples() -> WorkedExamples:
    return WorkedExamples.model_validate_json(WORKED_EXAMPLES_FILE.read_text(encoding="utf-8"))
