"""The run log's row-grain vocabulary, declared here rather than inline in the
driver that emits it. A run with no log passes None and every emitter is a
no-op, so the driver never branches on whether logging is on.
"""
from __future__ import annotations

from typing import Any

from ..run_log import ROW_ERROR, ROW_OK, ROW_START, SOURCE_CACHED, SOURCE_COMPUTED, RunLog


def emit_row_start(log: RunLog | None, stage_id: str, index: int) -> None:
    if log is not None:
        log.emit({"kind": ROW_START, "stage": stage_id, "row": index})


def emit_row_outcome(log: RunLog | None, stage_id: str, index: int, error: Any) -> None:
    """An LLM mapper tags a failed row instead of raising, so `error` is how a failure reaches here."""
    if log is None:
        return
    event = {"stage": stage_id, "row": index, "source": SOURCE_COMPUTED}
    if error is None:
        log.emit({"kind": ROW_OK, **event})
    else:
        log.emit({"kind": ROW_ERROR, "text": str(error), **event})


def emit_row_raised(log: RunLog | None, stage_id: str, index: int, exc: BaseException) -> None:
    if log is None:
        return
    log.emit({
        "kind": ROW_ERROR, "stage": stage_id, "row": index,
        "source": SOURCE_COMPUTED, "text": f"{type(exc).__name__}: {exc}",
    })


def emit_cached_row(log: RunLog | None, stage_id: str, index: int) -> None:
    if log is not None:
        log.emit({
            "kind": ROW_OK, "stage": stage_id, "row": index, "source": SOURCE_CACHED,
        })
