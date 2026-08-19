from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from threading import Lock

from app.core.latest_value import LatestValueBuffer
from app.models.run_manifest import StageProgress, StageRecord


class StageProgressReporter:
    def __init__(
        self,
        record: StageRecord | None = None,
        persist: Callable[[], None] = lambda: None,
        *,
        persist_interval_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._record = record
        self._progress: StageProgress | None = None if record is None else record.progress
        self._lock = Lock()
        self._values = LatestValueBuffer(
            self._persist_progress,
            interval_seconds=persist_interval_seconds,
            clock=clock,
        )
        self._persist = persist

    def __call__(self, *, completed: int, total: int | None) -> None:
        self.report(completed=completed, total=total)

    def start(self, *, total: int | None) -> None:
        with self._lock:
            self._values.persist_latest()
            progress = StageProgress(
                completed=0,
                total=total,
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._progress = progress
            self._values.append(progress)

    def has_started(self) -> bool:
        with self._lock:
            return self._progress is not None

    def report(self, *, completed: int, total: int | None) -> None:
        with self._lock:
            progress = StageProgress(
                completed=completed,
                total=total,
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            _validate_transition(self._progress, progress)
            self._progress = progress
            self._values.append(progress)

    def advance(self, count: int = 1) -> None:
        with self._lock:
            if count < 0:
                raise ValueError(f"progress advance must be non-negative, got {count}")
            if self._progress is None:
                raise ValueError("progress must be initialized before it can advance")
            progress = StageProgress(
                completed=self._progress.completed + count,
                total=self._progress.total,
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            _validate_transition(self._progress, progress)
            self._progress = progress
            self._values.append(progress)

    def persist_latest(self) -> bool:
        return self._values.persist_latest()

    def _persist_progress(self, progress: StageProgress) -> None:
        if self._record is not None:
            self._record.progress = progress
        self._persist()


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
