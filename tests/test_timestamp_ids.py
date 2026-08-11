"""What run ids, version ids and workflow-test run ids all inherit from mint_timestamp_id."""
from __future__ import annotations

from app.core.timestamp_ids import mint_timestamp_id

# The form ids on disk were minted in before this module existed. Every project's
# runs/ directory and every stored WorkflowVersion still carries ids like this.
_SECOND_RESOLUTION_ID = "20260810T213500"


def test_ids_minted_back_to_back_are_distinct():
    minted = [mint_timestamp_id() for _ in range(200)]
    assert len(set(minted)) == len(minted)


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
