from __future__ import annotations

from pydantic import ValidationError

from critic.backend import BackendReplyError, ModelBackend
from critic.diff import PullRequestDiff
from critic.predictions import PredictedComment, ReviewAnswer
from critic.prompt import build_review_request
from critic.records import CriticRecord
from critic.rubric import Rubric
from critic.themes import Theme
from critic.worked_examples import load_worked_examples


class Review(CriticRecord):
    pr: int
    commit_sha: str
    rubric: str
    model_ids: list[str]
    cost_usd: float
    duration_ms: int
    theme_vocabulary: list[str] | None
    predictions: list[PredictedComment]


def run_review(
    diff: PullRequestDiff, rubric: Rubric, themes: list[Theme] | None, backend: ModelBackend
) -> Review:
    examples = load_worked_examples().review
    reply = backend.ask(build_review_request(diff, rubric, themes, examples))
    try:
        answer = ReviewAnswer.model_validate(reply.answer)
    except ValidationError as error:
        raise BackendReplyError(f"the review answer does not fit its schema: {error}") from error
    slugs = None if themes is None else [theme.slug for theme in themes]
    check_themes_in_vocabulary(answer.comments, slugs)
    return Review(
        pr=diff.number,
        commit_sha=diff.commit_sha,
        rubric=rubric.name,
        model_ids=reply.model_ids,
        cost_usd=reply.cost_usd,
        duration_ms=reply.duration_ms,
        theme_vocabulary=slugs,
        predictions=answer.comments,
    )


def check_themes_in_vocabulary(comments: list[PredictedComment], slugs: list[str] | None) -> None:
    if slugs is None:
        return
    strays = sorted({comment.theme for comment in comments} - set(slugs))
    if strays:
        raise BackendReplyError(f"the model answered with themes outside the vocabulary: {strays}")
