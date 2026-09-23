"""What run ids, version ids and workflow-test run ids all inherit from mint_timestamp_id."""
from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Self

from app.core import timestamp_ids
from app.core.timestamp_ids import mint_timestamp_id

# The form ids on disk were minted in before this module existed. Every project's
# runs/ directory and every stored WorkflowVersion still carries ids like this.
_SECOND_RESOLUTION_ID = "20260810T213500"


def test_ids_minted_back_to_back_are_distinct():
    minted = [mint_timestamp_id() for _ in range(200)]
    assert len(set(minted)) == len(minted)


class _ClockStuckInOneTick(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        # In the past: stamps never go backwards, so a future one would carry into later tests.
        return cls(2026, 8, 10, 21, 35)


def test_ids_minted_within_one_clock_tick_are_distinct(monkeypatch):
    monkeypatch.setattr(timestamp_ids, "datetime", _ClockStuckInOneTick)
    assert mint_timestamp_id() != mint_timestamp_id()


def test_ids_sort_into_the_order_they_were_minted():
    minted = [mint_timestamp_id() for _ in range(200)]
    assert sorted(minted) == minted


def test_ids_are_fixed_width_so_a_string_sort_is_chronological():
    assert len({len(mint_timestamp_id()) for _ in range(200)}) == 1


def test_a_legacy_second_resolution_id_sorts_before_ids_minted_later_that_second():
    # Both list_versions and the newest-run lookup order these by plain string sort.
    same_second = f"{_SECOND_RESOLUTION_ID}.000001"
    next_second = "20260810T213501.000000"
    assert _SECOND_RESOLUTION_ID < same_second < next_second


def test_a_legacy_id_sorts_before_every_id_minted_now():
    assert _SECOND_RESOLUTION_ID < mint_timestamp_id()
