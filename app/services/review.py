# Recording a decision is the one sanctioned way run activity persists something
# that outlives the run.
from __future__ import annotations

from collections.abc import Mapping

from app.core.errors import ReviewValidationError
from app.core.stage_cache import StageCacheEntry
from app.models import Stage
from app.models.stages.human_review_queue import (
    QueueConfig,
    ReviewVerdict,
    resolve_queue_config,
)


def resolve_verdict(
    supplied: Mapping[str, str | None], prefilled: Mapping[str, str | None]
) -> ReviewVerdict:
    """`modify` iff a submitted value differs from what THE PAGE carried as its prefill."""
    # Deliberately not compared against a server-side recompute of the prefill: the
    # reviewer decided against what they were shown, and a decision landing between
    # render and submit would change what a recompute produced. A reviewer who
    # retypes an identical value records `approve` — `modify` means the value changed.
    unmatched = sorted(set(supplied) ^ set(prefilled))
    if unmatched:
        raise ReviewValidationError(
            "reviewed_values and prefilled_values must name the same columns — "
            f"the verdict is settled by comparing them; {unmatched} appears in only "
            "one of the two"
        )
    changed = any(value != prefilled[target] for target, value in supplied.items())
    return ReviewVerdict.modify if changed else ReviewVerdict.approve


def record_decision(
    *, project: str, stage: Stage,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: ReviewVerdict, reviewed_values: Mapping[str, object],
    review_notes: str | None,
    reviewer: str, reviewed_at: str,
) -> None:
    """`reviewed_values` is keyed by TARGET column name, already coerced by the caller."""
    # This validates the key set against what the queue block declares, not the
    # value types.
    queue = _require_queue_config(stage)
    _validate_verdict_came_from_a_human(verdict)
    _validate_reviewed_values_match_declared_columns(queue, reviewed_values)
    _validate_notes_match_declared_column(queue, review_notes)
    StageCacheEntry.read_write().record(
        project=project, stage_id=stage.id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        input_row=frozen_row,
        output_row=_build_output_row(
            queue, frozen_row, verdict, reviewed_values, review_notes, reviewer, reviewed_at
        ),
    )


def _require_queue_config(stage: Stage) -> QueueConfig:
    queue = resolve_queue_config(stage)
    if queue is None:
        raise ReviewValidationError(
            f"stage '{stage.id}' declares no queue config: it is a {stage.type} stage, "
            "not a human_review_queue"
        )
    return queue


def _validate_verdict_came_from_a_human(verdict: ReviewVerdict) -> None:
    if verdict == ReviewVerdict.skipped:
        raise ReviewValidationError(
            f"verdict '{verdict.value}' is the runtime's own: it records that no human saw "
            "the row, so no reviewer may post it"
        )


def _validate_reviewed_values_match_declared_columns(
    queue: QueueConfig, reviewed_values: Mapping[str, object]
) -> None:
    declared = set(queue.reviewed_columns.values())
    supplied = set(reviewed_values)
    missing = sorted(declared - supplied)
    unknown = sorted(supplied - declared)
    if missing or unknown:
        raise ReviewValidationError(
            f"reviewed_values must name exactly the declared reviewed columns "
            f"{sorted(declared)}: missing {missing}, unknown {unknown}"
        )


def _validate_notes_match_declared_column(queue: QueueConfig, review_notes: str | None) -> None:
    if review_notes is not None and queue.review_notes_column is None:
        raise ReviewValidationError(
            "review_notes was supplied but the stage declares no review_notes_column"
        )


def _build_output_row(
    queue: QueueConfig,
    frozen_row: Mapping[str, object],
    verdict: ReviewVerdict,
    reviewed_values: Mapping[str, object],
    review_notes: str | None,
    reviewer: str,
    reviewed_at: str,
) -> Mapping[str, object]:
    output_row: dict[str, object] = {
        **frozen_row,
        **reviewed_values,
        queue.verdict_column: verdict.value,
        queue.reviewer_column: reviewer,
        queue.reviewed_at_column: reviewed_at,
    }
    if queue.review_notes_column is not None:
        output_row[queue.review_notes_column] = review_notes
    return output_row
