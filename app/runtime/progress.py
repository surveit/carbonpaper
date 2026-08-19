from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from threading import Lock

from app.models.run_manifest import StageProgress, StageRecord


class StageProgressTracker:
    def __init__(
        self,
        record: StageRecord | None,
        flush: Callable[[], None],
        *,
        flush_interval_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._record = record
        self._flush = flush
        self._flush_interval_seconds = flush_interval_seconds
        self._clock = clock
        self._progress: StageProgress | None = None if record is None else record.progress
        self._last_flush: float | None = None
        self._lock = Lock()

    @classmethod
    def detached(cls) -> StageProgressTracker:
        return cls(None, lambda: None)

    def __call__(self, *, completed: int, total: int | None, unit: str) -> None:
        self.report(completed=completed, total=total, unit=unit)

    def report(self, *, completed: int, total: int | None, unit: str) -> None:
        with self._lock:
            progress = StageProgress(
                completed=completed,
                total=total,
                unit=unit,
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            _validate_transition(self._progress, progress)
            self._progress = progress
            if self._record is not None:
                self._record.progress = progress
            self._flush_if_due()

    def advance(self, count: int = 1) -> None:
        with self._lock:
            if count < 0:
                raise ValueError(f"progress advance must be non-negative, got {count}")
            if self._progress is None:
                raise ValueError("progress must be initialized before it can advance")
            progress = StageProgress(
                completed=self._progress.completed + count,
                total=self._progress.total,
                unit=self._progress.unit,
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            _validate_transition(self._progress, progress)
            self._progress = progress
            if self._record is not None:
                self._record.progress = progress
            self._flush_if_due()

    def flush(self) -> None:
        with self._lock:
            self._flush()
            self._last_flush = self._clock()

    def _flush_if_due(self) -> None:
        now = self._clock()
        if (
            self._last_flush is None
            or now - self._last_flush >= self._flush_interval_seconds
        ):
            self._flush()
            self._last_flush = now


def _validate_transition(
    previous: StageProgress | None, current: StageProgress
) -> None:
    if previous is None:
        return
    if current.unit != previous.unit:
        raise ValueError(
            f"progress unit changed from {previous.unit!r} to {current.unit!r}"
        )
    if current.completed < previous.completed:
        raise ValueError(
            f"progress regressed from {previous.completed} to {current.completed}"
        )
    if previous.total is not None and current.total != previous.total:
        raise ValueError(
            f"progress total changed from {previous.total} to {current.total}"
        )
