"""The read-only decision seam app/runtime holds. docs/run-manifest.md"""
from __future__ import annotations

from typing import NamedTuple

from app.core.json_types import JsonDict
from app.models.records.review_decision import ReviewDecision


class DecidedRow(NamedTuple):
    """One row's latest recorded decision, already resolved — never a correction to chase further."""

    verdict: str
    reviewed_values: JsonDict
    reviewer: str
    reviewed_at: str
    review_notes: str | None


class ReviewLedger:
    def __init__(self, project_id: str) -> None:
        self._project_id = project_id

    def find_recorded_decisions(
        self, stage_id: str, stage_fingerprint: str
    ) -> dict[str, DecidedRow]:
        """One bulk read per stage execution, keyed by input fingerprint — mirrors the cache's own."""
        decisions = ReviewDecision.find(
            project=self._project_id, stage_id=stage_id, stage_fingerprint=stage_fingerprint,
        )
        latest_by_key: dict[str, ReviewDecision] = {}
        for decision in decisions:
            current = latest_by_key.get(decision.input_fingerprint)
            if current is None or decision.created_at > current.created_at:
                latest_by_key[decision.input_fingerprint] = decision
        return {key: _as_decided_row(decision) for key, decision in latest_by_key.items()}


def _as_decided_row(decision: ReviewDecision) -> DecidedRow:
    return DecidedRow(
        verdict=decision.verdict,
        reviewed_values=decision.reviewed_values,
        reviewer=decision.reviewer,
        reviewed_at=decision.reviewed_at,
        review_notes=decision.review_notes,
    )
