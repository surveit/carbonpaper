from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from threading import Lock

from app.models.run_manifest import StageProgress, StageRecord


class StageProgressReporter:
    def __init__(
        self,
        record: StageRecord | None = None,
        write_manifest: Callable[[], None] = lambda: None,
        *,
        write_interval_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._record = record
        self._write_manifest = write_manifest
        self._write_interval_seconds = write_interval_seconds
        self._clock = clock
        self._last_write_at: float | None = None
        self._lock = Lock()

    def __call__(self, *, completed: int, total: int | None) -> None:
        with self._lock:
            progress = StageProgress(
                completed=completed,
                total=total,
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            if self._record is None:
                return
            _validate_transition(self._record.progress, progress)
            self._record.progress = progress
            self._write_if_due()

    def advance(self, count: int = 1) -> None:
        with self._lock:
            if count < 0:
                raise ValueError(f"progress advance must be non-negative, got {count}")
            if self._record is None:
                return
            if self._record.progress is None:
                raise ValueError("progress must be initialized before it can advance")
            progress = StageProgress(
                completed=self._record.progress.completed + count,
                total=self._record.progress.total,
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._record.progress = progress
            self._write_if_due()

    def finish(self) -> None:
        with self._lock:
            self._write()

    def _write_if_due(self) -> None:
        now = self._clock()
        if (
            self._last_write_at is None
            or now - self._last_write_at >= self._write_interval_seconds
        ):
            self._write(now)

    def _write(self, now: float | None = None) -> None:
        self._write_manifest()
        self._last_write_at = self._clock() if now is None else now


def _validate_transition(
    previous: StageProgress | None, current: StageProgress
) -> None:
    if previous is None:
        return
    if current.completed < previous.completed:
        raise ValueError(
            f"progress regressed from {previous.completed} to {current.completed}"
        )
    if previous.total is not None and current.total != previous.total:
        raise ValueError(
            f"progress total changed from {previous.total} to {current.total}"
        )
