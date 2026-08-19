from __future__ import annotations

import pytest

from app.core.latest_value import LatestValueBuffer


def test_first_value_persists_and_pending_values_are_replaced() -> None:
    now = [0.0]
    persisted: list[int] = []
    values = LatestValueBuffer(
        persisted.append,
        interval_seconds=0.5,
        clock=lambda: now[0],
    )

    values.append(0)
    now[0] = 0.1
    values.append(1)
    values.append(2)
    now[0] = 0.6
    values.append(3)

    assert persisted == [0, 3]


def test_persist_latest_writes_a_pending_value_without_a_timer() -> None:
    now = [0.0]
    persisted: list[int] = []
    values = LatestValueBuffer(
        persisted.append,
        interval_seconds=0.5,
        clock=lambda: now[0],
    )

    values.append(0)
    now[0] = 0.1
    values.append(1)
    assert values.persist_latest() is True
    assert values.persist_latest() is False

    assert persisted == [0, 1]


def test_persistence_failure_propagates_and_keeps_the_value_pending() -> None:
    attempts: list[int] = []

    def persist(value: int) -> None:
        attempts.append(value)
        if len(attempts) == 1:
            raise OSError("disk unavailable")

    values = LatestValueBuffer(persist, interval_seconds=0.5)

    with pytest.raises(OSError, match="disk unavailable"):
        values.append(7)
    values.persist_latest()

    assert attempts == [7, 7]
