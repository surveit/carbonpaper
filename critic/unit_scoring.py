from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from critic.backend import BackendReplyError, ModelBackend
from critic.diff import DiffTooLargeError, PullRequestDiff, find_unanchored_comments, load_or_fetch_diff
from critic.gh import GitHubCliError
from critic.judge import JudgeOutcome, JudgeVerdict, judge_candidates
from critic.labels import ReviewerComment
from critic.matching import (
    CandidatePair,
    Matching,
    accept_greedily,
    assemble_matching,
    match_exactly,
    pair_same_path,
)
from critic.planning import ReviewUnit
from critic.predictions import PredictedComment
from critic.pr_history import UnknownBodyError, find_body_at, find_title_at
from critic.records import CriticRecord
from critic.review import Review, run_review
from critic.rubric import Rubric
from critic.scoring import ALL_ROUTES, Score, score_unit

UnscoredStatus = Literal["skipped", "failed"]


class ScoredUnit(CriticRecord):
    pr: int
    commit_sha: str
    reals: list[ReviewerComment]
    review: Review
    matching: Matching
    judge_verdicts: list[JudgeVerdict]
    judge_cost_usd: float
    judge_model_ids: list[str]
    score: Score


class UnscoredUnit(CriticRecord):
    pr: int
    commit_sha: str
    status: UnscoredStatus
    reason: str
    reals: list[ReviewerComment]


@dataclass(frozen=True)
class UnitContext:
    repo: str
    diff_dir: Path
    rubric: Rubric
    review_backend: ModelBackend
    judge_backend: ModelBackend


def evaluate_unit(unit: ReviewUnit, context: UnitContext) -> ScoredUnit | UnscoredUnit:
    try:
        diff = load_or_fetch_diff(context.repo, unit.pr, unit.commit_sha, context.diff_dir)
        same_commit = [real for real in unit.reals if real.commit_sha == unit.commit_sha]
        unanchored = find_unanchored_comments(same_commit, diff)
        if unanchored:
            reason = f"the diff rebuilt at this commit lacks the line of {unanchored}"
            return _leave_unscored(unit, "skipped", reason)
        diff_as_read = rewind_to_review(diff, unit)
        review = run_review(diff_as_read, context.rubric, unit.themes, context.review_backend)
        return score_review(unit, review, diff_as_read, context.judge_backend)
    except (BackendReplyError, GitHubCliError, DiffTooLargeError, UnknownBodyError) as error:
        return _leave_unscored(unit, "failed", f"{type(error).__name__}: {error}")


def rewind_to_review(diff: PullRequestDiff, unit: ReviewUnit) -> PullRequestDiff:
    """Title and description as they stood at review time: later edits may answer the review."""
    title = find_title_at(unit.history, diff.title, unit.reviewed_at)
    body = find_body_at(unit.history, diff.body, unit.reviewed_at)
    return diff.model_copy(update={"title": title, "body": body})


def score_review(
    unit: ReviewUnit, review: Review, diff: PullRequestDiff, judge_backend: ModelBackend
) -> ScoredUnit:
    predictions = review.predictions
    exact = match_exactly(predictions, unit.reals, unit.commit_sha)
    candidates = pair_same_path(predictions, unit.reals, taken=exact)
    judged = _judge(candidates, predictions, unit.reals, diff, judge_backend)
    reasons = {verdict.pair_id: verdict.reason for verdict in judged.verdicts if verdict.same_point}
    accepted = [candidate for candidate in candidates if candidate.pair_id in reasons]
    judged_pairs = accept_greedily(accepted, route="judge", reasons=reasons, taken=exact)
    matching = assemble_matching(exact + judged_pairs, len(predictions), len(unit.reals))
    return ScoredUnit(
        pr=unit.pr,
        commit_sha=unit.commit_sha,
        reals=unit.reals,
        review=review,
        matching=matching,
        judge_verdicts=judged.verdicts,
        judge_cost_usd=judged.cost_usd,
        judge_model_ids=judged.model_ids,
        score=score_unit(predictions, unit.reals, matching, ALL_ROUTES),
    )


def _judge(
    candidates: list[CandidatePair],
    predictions: list[PredictedComment],
    reals: list[ReviewerComment],
    diff: PullRequestDiff,
    judge_backend: ModelBackend,
) -> JudgeOutcome:
    if not candidates:
        return JudgeOutcome(verdicts=[], cost_usd=0.0, model_ids=[])
    return judge_candidates(candidates, predictions, reals, diff, judge_backend)


def _leave_unscored(unit: ReviewUnit, status: UnscoredStatus, reason: str) -> UnscoredUnit:
    return UnscoredUnit(
        pr=unit.pr, commit_sha=unit.commit_sha, status=status, reason=reason, reals=unit.reals
    )
