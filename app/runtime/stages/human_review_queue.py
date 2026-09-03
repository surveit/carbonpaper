"""Handler for the human_review_queue stage type. docs/run-manifest.md"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import pandas as pd
import pyarrow as pa

from app.core.frames import table_to_frame
from app.core.frames import write_frame_file_with_csv_fallback
from app.core.predicate import parse_predicate
from app.models import AbstractStage, WorkflowStage
from app.models.review_ledger import DecidedRow, ReviewLedger
from app.models.stage_contribution import QueueStats, StageContribution
from app.models.stages.human_review_queue import (
    HumanReviewQueueStage,
    QueueConfig,
    QueueSortKey,
    ReviewVerdict,
    SortDirection,
)
from app.core.stage_cache import ReadOnlyStageCache, StageCacheEntry, compute_row_fingerprint
from app.models.records.queue_fingerprints import QueueFingerprints

from ..context import RunContext, RunIdentity
from ..stage_output import AwaitingReview
from .execution import ROW_DEFERRED_KEY, Row, RowMapper, narrow_stage



@dataclass(frozen=True)
class PendingReview:
    input_fingerprint: str
    frozen_row: Row
    # 0-based position in this stage's INPUT frame — also its position in the
    # upstream stage's output frame, since a row-mapped stage neither reorders
    # nor fans out rows.
    row_ordinal: int


def build_human_review_mapper(
    workflow_stage: WorkflowStage, ctx: RunContext, src: pa.Table
) -> RowMapper:
    """The callable that decides one row's outcome for one execution of this stage."""
    queue_stage = narrow_stage(workflow_stage, HumanReviewQueueStage)
    queue = queue_stage.queue
    # The queue's source-column check reads rows, so this handler materializes at
    # its own edge.
    src_frame = table_to_frame(src)
    # The one place every path through this stage passes, so the frame is checked
    # against the declared columns before a row is mapped, a snapshot written or a
    # halt raised.
    validate_reviewed_sources_present(queue, src_frame, queue_stage.id)
    # Auto-approve is answered here and goes no further: `_approve_row` reaches for
    # no project scope, no cache, no disk and no filter, so a run carrying none of
    # those can still pass a queue stage through.
    if ctx.params.queue_auto_approve:
        return partial(_approve_row, queue)
    return _QueueRowMapper(queue_stage, queue, ctx, src_frame)


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
        identity, stage_cache, decisions = _require_project_scope(ctx, queue_stage.id)
        self._queueable = _compute_queueable_mask(src, queue.filter, queue_stage.id)
        self._decisions: dict[str, DecidedRow] = {}
        self._cached: dict[str, StageCacheEntry] = {}
        # bust_cache re-asks a human — a re-ask, never a loss: the ledger keeps every answer.
        if not ctx.params.bust_cache:
            stage_fingerprint = queue_stage.compute_definition_fingerprint()
            self._decisions = decisions.find_recorded_decisions(queue_stage.id, stage_fingerprint)
            self._cached = stage_cache.find_recorded_entries(
                identity.project, queue_stage.id, stage_fingerprint)

    def __call__(self, row: Row, index: int) -> Row:
        if not self._queueable[index]:
            return _skip_row(self._queue, row)
        decided = self._resolve_decided_row(row)
        if decided is not None:
            return decided
        return _defer_row(row, index)

    def _resolve_decided_row(self, row: Row) -> Row | None:
        """The ledger first: a cache entry imported from elsewhere must never outrank it."""
        fingerprint = compute_row_fingerprint(row)
        decision = self._decisions.get(fingerprint)
        if decision is not None:
            return self._queue.build_reviewed_row(
                row, verdict=decision.verdict, reviewed_values=decision.reviewed_values,
                reviewer=decision.reviewer, reviewed_at=decision.reviewed_at,
                review_notes=decision.review_notes,
            )
        entry = self._cached.get(fingerprint)
        if entry is not None and entry.output_row is not None:
            return dict(entry.output_row)
        return None

    def finish_mapped_rows(
        self,
        workflow_stage: WorkflowStage,
        rows: Sequence[Row],
        ctx: RunContext,
        contribution: StageContribution,
    ) -> AwaitingReview | None:
        # The stats and pending scan are pandas work; materialize at this edge.
        df = pd.DataFrame(list(rows))
        stage = workflow_stage.stage
        contribution.human_review_queue_stats = _compute_queue_stats(self._queue, df)
        pending = _order_pending_reviews(self._queue.sort, _find_pending_reviews(df), stage.id)
        if not pending:
            return None
        queue_path = _write_queue_files(
            ctx.require_run_dir() / "queue", ctx.require_identity(),
            workflow_stage, pending)
        return AwaitingReview(
            stage_id=stage.id,
            pending_count=len(pending),
            queue_path=queue_path,
        )


# --- _QueueRowMapper.__init__: once per stage execution ------------------------


def _require_project_scope(
    ctx: RunContext, sid: str
) -> tuple[RunIdentity, ReadOnlyStageCache, ReviewLedger]:
    if ctx.identity is None or ctx.stage_cache is None or ctx.decisions is None:
        raise ValueError(
            f"human_review_queue '{sid}' requires a project-scoped (production) "
            "run: RunContext.identity, RunContext.stage_cache and "
            "RunContext.decisions must all be set, but this run carries none of them."
        )
    return ctx.identity, ctx.stage_cache, ctx.decisions


def _compute_queueable_mask(src: pd.DataFrame, flt: str | None, sid: str) -> list[bool]:
    """A filter that will not evaluate must raise: defaulting the mask would skip review unnoticed."""
    if not flt:
        return [True] * len(src)
    parsed = parse_predicate(flt, src.columns)
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
    reviewed_values = {target: row[source] for source, target in queue.reviewed_columns.items()}
    return queue.build_reviewed_row(
        row, verdict=verdict.value, reviewed_values=reviewed_values,
        reviewer=pd.NA, reviewed_at=pd.NA, review_notes=pd.NA,
    )


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
    queue_dir: Path, identity: RunIdentity, workflow_stage: WorkflowStage,
    pending: list[PendingReview],
) -> Path:
    """Write the snapshot frame and store its fingerprints; return the frame's path."""
    stage = workflow_stage.stage
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = _write_pending_snapshot(queue_dir, stage.id, pending)
    _store_fingerprints(identity, stage, pending)
    return queue_path


def _write_pending_snapshot(queue_dir: Path, sid: str, pending: list[PendingReview]) -> Path:
    frame = pd.DataFrame([item.frozen_row for item in pending])
    return write_frame_file_with_csv_fallback(frame, queue_dir / f"{sid}.parquet").path


def _store_fingerprints(
    identity: RunIdentity, stage: AbstractStage, pending: list[PendingReview]
) -> None:
    """`input_fingerprints` and `row_ordinals` are POSITIONALLY aligned to the snapshot."""
    QueueFingerprints(
        id=QueueFingerprints.compose_id(identity.project, identity.run_id, stage.id),
        stage_fingerprint=stage.compute_definition_fingerprint(),
        input_fingerprints=[item.input_fingerprint for item in pending],
        row_ordinals=[item.row_ordinal for item in pending],
    ).save()
