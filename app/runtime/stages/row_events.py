"""The run log's row-grain vocabulary, for the two row-driven execution paths.

Both the per-row driver and the batched llm_transform path report the same row
lifecycle, so the shape of those events is declared once, here, rather than
twice in `execution.py`.
"""
from __future__ import annotations

from typing import Any, Iterable

from ..run_log import ROW_ERROR, ROW_OK, ROW_START, SOURCE_CACHED, SOURCE_COMPUTED, RunLog


def emit_row_start(log: RunLog, stage_id: str, index: int) -> None:
    log.emit({"kind": ROW_START, "stage": stage_id, "row": index})


def emit_row_outcome(log: RunLog, stage_id: str, index: int, error: Any) -> None:
    """One computed row's terminal event; `error` is what the mapper tagged it with."""
    # An LLM row mapper does NOT raise on a failed row — it tags the row and
    # returns it — so a swallowed generation failure would otherwise log as a
    # success. `is None` is the same test `_record_row_output` uses.
    event = {"stage": stage_id, "row": index, "source": SOURCE_COMPUTED}
    if error is None:
        log.emit({"kind": ROW_OK, **event})
    else:
        log.emit({"kind": ROW_ERROR, "text": str(error), **event})


def emit_row_raised(log: RunLog, stage_id: str, index: int, exc: BaseException) -> None:
    log.emit({
        "kind": ROW_ERROR, "stage": stage_id, "row": index,
        "source": SOURCE_COMPUTED, "text": f"{type(exc).__name__}: {exc}",
    })


def emit_cached_row(log: RunLog | None, stage_id: str, index: int) -> None:
    """A replayed row: ONE terminal event, marked cached."""
    # No row_start and no LLM detail follow, because nothing ran — that absence
    # is the honest record, so a replayed row never reads as a computed one.
    if log is not None:
        log.emit({
            "kind": ROW_OK, "stage": stage_id, "row": index, "source": SOURCE_CACHED,
        })


def emit_batched_row_starts(
    log: RunLog | None, stage_id: str, hits: Iterable[int], misses: Iterable[int]
) -> None:
    """Every cache hit settles now (it computes nothing); every miss opens."""
    if log is None:
        return
    for position in sorted(hits):
        emit_cached_row(log, stage_id, position)
    for position in misses:
        emit_row_start(log, stage_id, position)


def emit_batched_row_outcomes(
    log: RunLog | None, stage_id: str, misses: Iterable[int], errors: Iterable[Any]
) -> None:
    """One terminal event per computed row, `errors` in the same order as `misses`."""
    # The caller has already raised unless exactly one computed row came back per
    # miss, so this zip cannot silently mis-pair a row with another row's error.
    if log is None:
        return
    for position, error in zip(misses, errors):
        emit_row_outcome(log, stage_id, position, error)
