from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock
from typing import Generic, TypeVar, cast


T = TypeVar("T")
_EMPTY = object()


class LatestValueBuffer(Generic[T]):
    def __init__(
        self,
        persist: Callable[[T], None],
        *,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._persist = persist
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._pending: T | object = _EMPTY
        self._last_persisted_at: float | None = None
        self._lock = Lock()

    def append(self, value: T) -> None:
        with self._lock:
            self._pending = value
            now = self._clock()
            if (
                self._last_persisted_at is None
                or now - self._last_persisted_at >= self._interval_seconds
            ):
                self._persist_pending(now)

    def persist_latest(self) -> bool:
        with self._lock:
            if self._pending is _EMPTY:
                return False
            self._persist_pending(self._clock())
            return True

    def _persist_pending(self, now: float) -> None:
        assert self._pending is not _EMPTY
        self._persist(cast(T, self._pending))
        self._pending = _EMPTY
        self._last_persisted_at = now
