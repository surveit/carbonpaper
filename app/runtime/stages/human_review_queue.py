"""Handler for the human_review_queue stage type.

Every input row yields exactly one output row in its own input position; a row with
no cached decision is marked deferred, never defaulted. On any deferral the mapper
writes a fingerprints sidecar POSITIONALLY aligned to the snapshot's rows, and halts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pandas as pd

from app.core.frames import write_frame_file_with_csv_fallback
from app.core.predicate import parse_predicate
from app.models import WorkflowStage
from app.models.run_manifest import QueueStats, StageContribution
from app.models.stages.human_review_queue import (
    HumanReviewQueueStage,
    QueueConfig,
    QueueSortKey,
    ReviewVerdict,
    SortDirection,
)
from app.core.stage_cache import compute_row_fingerprint

from ..context import RunContext
from ..errors import HaltForReview
from .execution import ROW_DEFERRED_KEY, Row, RowMapper, narrow_stage


@dataclass(frozen=True)
class PendingReview:
    input_fingerprint: str
    frozen_row: Row
    # 0-based position in this stage's INPUT frame — also its position in the
    # upstream stage's output frame, since a row-mapped stage neither reorders
    # nor fans out rows.
    row_ordinal: int


def make_human_review_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pd.DataFrame
) -> RowMapper:
    queue_stage = narrow_stage(workflow_stage, HumanReviewQueueStage)
    queue = queue_stage.queue
    validate_reviewed_sources_present(queue, src, queue_stage.id)
    if ctx.params.queue_auto_approve:
        return partial(_approve_row, queue)
    return _QueueRowMapper(queue_stage, queue, ctx, src)


def validate_reviewed_sources_present(
    queue: QueueConfig, src: pd.DataFrame, sid: str
) -> None:
    """Without this, a frame missing a reviewed column would halt for review instead of failing."""
    missing = sorted(set(queue.reviewed_columns) - set(src.columns))
    if missing:
        raise ValueError(
            f"human_review_queue '{sid}': queue.reviewed_columns names source column(s) "
            f"{missing}, which this stage's actual input frame does not carry "
            f"(it has {sorted(src.columns)}). The frame does not match the schema the "
            "stage declares — no value may stand in for a missing column."
        )


class _QueueRowMapper:
    def __init__(
        self, queue_stage: HumanReviewQueueStage, queue: QueueConfig, ctx: RunContext,
        src: pd.DataFrame,
    ) -> None:
        self._queue = queue
        _require_project_scope(ctx, queue_stage.id)
        self._queueable = _compute_queueable_mask(src, queue.filter, queue_stage.id)

    def __call__(self, row: Row, index: int) -> Row:
        if not self._queueable[index]:
            return _skip_row(self._queue, row)
        return _defer_row(row, index)

    def finish_mapped_rows(
        self,
        workflow_stage: WorkflowStage,
        df: pd.DataFrame,
        ctx: RunContext,
        contribution: StageContribution,
    ) -> None:
        stage = workflow_stage.stage
        contribution.human_review_queue_stats = _compute_queue_stats(self._queue, df)
        pending = _order_pending_reviews(self._queue.sort, _find_pending_reviews(df), stage.id)
        if not pending:
            return
        queue_path = _write_queue_files(
            ctx.require_run_dir() / "queue", workflow_stage, pending)
        raise HaltForReview(
            stage_id=stage.id,
            pending_count=len(pending),
            queue_path=queue_path,
            contribution=contribution,
        )


# --- _QueueRowMapper.__init__: once per stage execution ------------------------


def _require_project_scope(ctx: RunContext, sid: str) -> None:
    if ctx.identity is None or ctx.stage_cache is None:
        raise ValueError(
            f"human_review_queue '{sid}' requires a project-scoped (production) "
            "run: RunContext.identity and RunContext.stage_cache must both be "
            "set, but this run carries neither."
        )


def _compute_queueable_mask(src: pd.DataFrame, flt: str | None, sid: str) -> list[bool]:
    """A filter that will not evaluate must raise: defaulting the mask would skip review unnoticed."""
    if not flt:
        return [True] * len(src)
    parsed = parse_predicate(flt)
    try:
        # eval of a comparison yields a bool Series; the explicit dtype=bool
        # conversion makes that a checked fact (anything else lands in the
        # except below and is raised as a loud error).
        mask = pd.Series(src.eval(parsed.pandas_expr), index=src.index, dtype=bool)
    except (SyntaxError, ValueError, TypeError, KeyError, AttributeError, NameError) as exc:
        raise ValueError(
            f"human_review_queue '{sid}' filter could not be evaluated: `{flt}` "
            f"({type(exc).__name__}: {exc}). A filter must reference existing input columns."
        ) from exc
    return [bool(verdict) for verdict in mask]


# --- the row outcomes the mapper does not need its own state for ---------------


def _defer_row(row: Row, index: int) -> Row:
    """The fingerprint must match the driver's row-cache key, or the decision never resolves."""
    return {
        ROW_DEFERRED_KEY: PendingReview(
            input_fingerprint=compute_row_fingerprint(row),
            frozen_row=dict(row),
            row_ordinal=index,
        )
    }


def _skip_row(queue: QueueConfig, row: Row) -> Row:
    return _add_review_columns(queue, row, ReviewVerdict.skipped)


def _approve_row(queue: QueueConfig, row: Row, index: int) -> Row:
    return _add_review_columns(queue, row, ReviewVerdict.approve)


def _add_review_columns(queue: QueueConfig, row: Row, verdict: ReviewVerdict) -> Row:
    added: Row = {
        target: row[source] for source, target in queue.reviewed_columns.items()
    }
    added[queue.verdict_column] = verdict.value
    added[queue.reviewer_column] = pd.NA
    added[queue.reviewed_at_column] = pd.NA
    if queue.review_notes_column is not None:
        added[queue.review_notes_column] = pd.NA
    return {**row, **added}


# --- finish_mapped_rows: the deferred rows, the snapshot and its sidecar ------


def _compute_queue_stats(queue: QueueConfig, df: pd.DataFrame) -> QueueStats:
    column = queue.verdict_column
    verdicts = list(df[column]) if column in df.columns else []
    passed_through = sum(1 for value in verdicts if value == ReviewVerdict.skipped)
    decided = sum(
        1 for value in verdicts
        if not pd.isna(value) and value != ReviewVerdict.skipped
    )
    pending = len(_find_pending_reviews(df))
    return {
        "items_queued_total": pending + decided,
        "items_passed_through": passed_through,
        "items_pending": pending,
        "items_decided": decided,
    }


def _find_pending_reviews(df: pd.DataFrame) -> list[PendingReview]:
    if ROW_DEFERRED_KEY not in df.columns:
        return []
    return [value for value in df[ROW_DEFERRED_KEY] if isinstance(value, PendingReview)]


# Permuting the pending list is what keeps the snapshot, its fingerprints and its
# row ordinals aligned: all three are written from this one list below, so the
# review order cannot drift from the decisions and lineage links it carries.
def _order_pending_reviews(
    sort: list[QueueSortKey], pending: list[PendingReview], sid: str
) -> list[PendingReview]:
    if not sort or not pending:
        return pending
    frame = pd.DataFrame([item.frozen_row for item in pending])  # index 0..n-1
    _require_sort_columns_present(sort, frame, sid)
    ordered = frame.sort_values(
        by=[key.column for key in sort],
        ascending=[key.direction == SortDirection.ascending for key in sort],
        kind="stable",
        na_position="last",  # a null is an absent value, so it leads no queue
    )
    return [pending[position] for position in ordered.index]


def _require_sort_columns_present(
    sort: list[QueueSortKey], frame: pd.DataFrame, sid: str
) -> None:
    missing = sorted({key.column for key in sort} - set(frame.columns))
    if missing:
        raise ValueError(
            f"human_review_queue '{sid}': queue.sort orders by column(s) {missing}, "
            f"which the queued rows do not carry (they have "
            f"{sorted(str(c) for c in frame.columns)}). No substitute order may stand "
            "in for the one the stage declares."
        )


def _write_queue_files(
    queue_dir: Path, workflow_stage: WorkflowStage, pending: list[PendingReview]
) -> Path:
    stage = workflow_stage.stage
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = _write_pending_snapshot(queue_dir, stage.id, pending)
    _write_fingerprint_sidecar(
        queue_dir, stage.id, stage.compute_definition_fingerprint(), pending
    )
    return queue_path


def _write_pending_snapshot(queue_dir: Path, sid: str, pending: list[PendingReview]) -> Path:
    frame = pd.DataFrame([item.frozen_row for item in pending])
    return write_frame_file_with_csv_fallback(frame, queue_dir / f"{sid}.parquet").path


def _write_fingerprint_sidecar(
    queue_dir: Path, sid: str, stage_fingerprint: str, pending: list[PendingReview]
) -> None:
    (queue_dir / f"{sid}.fingerprints.json").write_text(
        json.dumps({
            "stage_fingerprint": stage_fingerprint,
            "input_fingerprints": [item.input_fingerprint for item in pending],
            "row_ordinals": [item.row_ordinal for item in pending],
        }),
        encoding="utf-8",
    )
