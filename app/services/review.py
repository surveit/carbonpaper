"""The decision layer behind the reviewer web route: validate a submitted
verdict, write a reviewed row into the stage-result cache
(app.services.stage_cache), and load the prior decisions recorded against one
stage definition. HTTP and run-directory file reads stay in the route
(app.web.routers.review); everything decision-shaped lives here.

Writes and reads both go through the production cache accessor
(`StageCacheEntry.for_mode(RunMode.PRODUCTION)`): recording a decision is the
one sanctioned way run activity persists something that outlives the run."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.core.errors import ReviewValidationError
from app.core.persistence import JsonDict
from app.core.run_status import RunMode
from app.models import RowReviewDecision
from app.services.stage_cache import (
    CacheProvenance,
    StageCacheEntry,
    build_cache_id,
    to_json_safe_row,
)


@dataclass(frozen=True)
class PriorDecision:
    """A reviewer decision already recorded for one queued row, read back from
    the cache to render on the reviewer page."""

    decision: str            # verdict label: "approve" | "modify" | "reject"
    modified_score: float | None
    reviewer: str
    reviewed_at: str


def parse_verdict(decision: str, modified_score: str | None) -> tuple[RowReviewDecision, float | None]:
    """Validate a submitted verdict + optional score, returning the verdict and
    the parsed score (None unless `modify`). Raises ReviewValidationError on an
    unknown verdict or a `modify` with a missing/non-numeric score."""
    verdict = _parse_verdict_label(decision)
    score = _parse_modified_score(verdict, modified_score)
    return verdict, score


def record_decision(
    *, project: str, stage_id: str, run_id: str,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> None:
    """Build the cache entry for one reviewed row and write it through the
    production cache accessor (StageCacheEntry.for_mode(RunMode.PRODUCTION).put)."""
    entry = _build_cache_entry(
        project=project, stage_id=stage_id, run_id=run_id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_row=frozen_row, verdict=verdict, modified_score=modified_score,
        reviewer=reviewer, reviewed_at=reviewed_at,
    )
    StageCacheEntry.for_mode(RunMode.PRODUCTION).put(entry)


def load_prior_decisions(
    project: str, stage_id: str, stage_fingerprint: str
) -> dict[str, PriorDecision]:
    """Prior decisions for this stage definition, keyed by input_fingerprint."""
    entries = StageCacheEntry.for_mode(RunMode.PRODUCTION).find_entries(
        project, stage_id, stage_fingerprint
    )
    return {entry.input_fingerprint: _summarize_prior_decision(entry) for entry in entries}


def _summarize_prior_decision(entry: StageCacheEntry) -> PriorDecision:
    """A generic cache entry read back as the reviewer decision it records. The
    verdict label is `provenance.note` — reliable even for a tombstone, whose
    `output_row` is None; the modified score is the `final_score` the output row
    carries, but only for a `modify` (an `approve` kept the AI score, never a
    human override)."""
    is_modify = entry.provenance.note == RowReviewDecision.modify.value
    modified_score = (
        entry.output_row["final_score"]
        if entry.output_row is not None and is_modify
        else None
    )
    return PriorDecision(
        decision=entry.provenance.note,
        modified_score=modified_score,
        reviewer=entry.provenance.author,
        reviewed_at=entry.provenance.recorded_at,
    )


def _parse_verdict_label(decision: str) -> RowReviewDecision:
    if decision not in (RowReviewDecision.approve, RowReviewDecision.reject,
                        RowReviewDecision.modify):
        raise ReviewValidationError(f"unknown decision '{decision}'")
    return RowReviewDecision(decision)


def _parse_modified_score(verdict: RowReviewDecision, modified_score: str | None) -> float | None:
    if verdict != RowReviewDecision.modify:
        return None
    if not modified_score:
        raise ReviewValidationError("modify requires modified_score")
    try:
        return float(modified_score)
    except ValueError:
        raise ReviewValidationError("modified_score must be numeric") from None


def _build_cache_entry(
    *, project: str, stage_id: str, run_id: str,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> StageCacheEntry:
    """A `StageCacheEntry` for this reviewer decision, generalized to the cache's
    payload: fingerprints are the ones the caller resolved (never recomputed
    here), `frozen_input` the reviewed row reduced to JSON-native types, and
    `output_row` the review stage's output for it (None as a tombstone for a
    `reject`). The verdict itself survives only as `provenance.note`, so the
    cache stores no review vocabulary."""
    frozen_safe = to_json_safe_row({str(k): v for k, v in frozen_row.items()})
    output_row = _build_output_row(frozen_safe, verdict, modified_score, reviewer, reviewed_at)
    provenance = CacheProvenance(author=reviewer, recorded_at=reviewed_at, note=verdict.value)
    return StageCacheEntry(
        id=build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint),
        project=project,
        stage_id=stage_id,
        stage_fingerprint=stage_fingerprint,
        input_fingerprint=input_fingerprint,
        source_run_id=run_id,
        frozen_input=frozen_safe,
        output_row=output_row,
        provenance=provenance,
    )


def _build_output_row(
    frozen_safe: JsonDict, verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> JsonDict | None:
    """The review stage's output row for one reviewed input: the frozen input
    plus the score columns the verdict produces. A `reject` drops the row, so
    its output is None (a tombstone); `approve` keeps the AI score as final,
    `modify` substitutes the human-entered score."""
    if verdict == RowReviewDecision.reject:
        return None
    ai = frozen_safe.get("score")
    human = modified_score if verdict == RowReviewDecision.modify else ai
    return {
        **frozen_safe,
        "ai_score": ai, "human_score": human, "final_score": human,
        "review_notes": f"decision={verdict.value}",
        "reviewer_id": reviewer, "reviewed_at": reviewed_at,
        "decision": verdict.value,
    }
