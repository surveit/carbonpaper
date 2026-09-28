from __future__ import annotations

from critic.corpus import CorpusIndex
from critic.labels import (
    ExcludedLabel,
    LabeledComment,
    ReviewerComment,
    select_reviewer_comments,
)
from critic.pr_history import PrHistory, fetch_pr_history, find_base_changes_after
from critic.records import CriticRecord
from critic.themes import Theme, select_flag_themes


class ReviewUnit(CriticRecord):
    """One PR, read at the commit of the reviewer's first ask whose diff can be rebuilt."""

    pr: int
    commit_sha: str
    reviewed_at: str
    reals: list[ReviewerComment]
    themes: list[Theme]
    history: PrHistory


class SkippedPr(CriticRecord):
    pr: int
    reason: str


class EvalPlan(CriticRecord):
    units: list[ReviewUnit]
    skipped_prs: list[SkippedPr]
    excluded_labels: list[ExcludedLabel]


def plan_units(
    repo: str,
    pr_numbers: list[int],
    limit: int | None,
    corpus: CorpusIndex,
    labels_by_pr: dict[int, list[LabeledComment]],
    themes: list[Theme],
) -> EvalPlan:
    plans: list[EvalPlan] = []
    for pr in pr_numbers:
        if limit is not None and sum(len(plan.units) for plan in plans) >= limit:
            break
        plans.append(plan_pr(repo, pr, corpus, labels_by_pr.get(pr, []), themes))
    return EvalPlan(
        units=[unit for plan in plans for unit in plan.units],
        skipped_prs=[skipped for plan in plans for skipped in plan.skipped_prs],
        excluded_labels=[label for plan in plans for label in plan.excluded_labels],
    )


def plan_pr(
    repo: str, pr: int, corpus: CorpusIndex, labels: list[LabeledComment], themes: list[Theme]
) -> EvalPlan:
    if not labels:
        return _skip_pr(pr, "the label set holds no comment on this PR", [])
    truth = select_reviewer_comments(labels, corpus, themes)
    if not truth.comments:
        return _skip_pr(pr, "no inline comment by the reviewer that asks for something", truth.excluded)
    history = fetch_pr_history(repo, pr)
    asks = sorted(truth.comments, key=lambda comment: comment.created_at)
    readable = [ask for ask in asks if not find_base_changes_after(history, ask.created_at)]
    unreadable = [_exclude_before_base_change(ask, history) for ask in asks if ask not in readable]
    excluded = truth.excluded + unreadable
    if not readable:
        return _skip_pr(pr, "every ask predates a base-branch change, so no diff can be rebuilt", excluded)
    unit = build_unit(readable, select_flag_themes(themes), history)
    return EvalPlan(units=[unit], skipped_prs=[], excluded_labels=excluded)


def build_unit(asks: list[ReviewerComment], themes: list[Theme], history: PrHistory) -> ReviewUnit:
    first = asks[0]
    return ReviewUnit(
        pr=first.pr,
        commit_sha=first.commit_sha,
        reviewed_at=first.created_at,
        reals=asks,
        themes=themes,
        history=history,
    )


def _exclude_before_base_change(ask: ReviewerComment, history: PrHistory) -> ExcludedLabel:
    changes = find_base_changes_after(history, ask.created_at)
    reason = f"left before the base branch changed at {changes[0]}, so its diff cannot be rebuilt"
    return ExcludedLabel(html_url=ask.html_url, reason=reason)


def _skip_pr(pr: int, reason: str, excluded: list[ExcludedLabel]) -> EvalPlan:
    return EvalPlan(units=[], skipped_prs=[SkippedPr(pr=pr, reason=reason)], excluded_labels=excluded)
