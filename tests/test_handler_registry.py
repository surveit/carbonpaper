"""Each stage type's registered handler must report the same grain-and-order
preservation the core model declares for that type. The registry import already
enforces this via validate_registry_matches_model; these tests pin the equality
of `handler.preserves_grain_and_order` with `is_grain_and_order_preserving`, and
the total coverage of stage types, as named, discoverable checks."""
from __future__ import annotations

from app.models.stage import (
    StageType,
    find_positional_cross,
    is_grain_and_order_preserving,
)
from app.runtime.stages import HANDLERS, RowMapHandler


def test_human_review_queue_maps_one_row_at_a_time_so_its_shared_counters_stay_correct():
    """human_review_queue's mapper increments ONE shared QueueStats dict with
    `+=` — a non-atomic load/add/store. Raising this stage's parallelism would
    let two rows read the same count and write it back once, under-reporting
    the run manifest's queue counts with nothing failing and no exception. That
    is a silently wrong number, so the invariant is pinned here rather than
    left to a comment."""
    handler = HANDLERS[StageType.human_review_queue]
    assert isinstance(handler, RowMapHandler)
    assert handler.parallelism == 1


def test_registry_shape_matches_the_model_for_every_type():
    for stage_type, handler in HANDLERS.items():
        assert handler.preserves_grain_and_order == is_grain_and_order_preserving(stage_type), stage_type


def test_every_stage_type_has_a_handler():
    assert set(HANDLERS) == set(StageType)


def test_every_row_driven_type_that_takes_input_is_positionally_crossable():
    """A type the runtime drives row by row has output row i from input row i,
    so the tracer must be able to cross it. Declaring a new row-driven type and
    forgetting find_positional_cross would silently shorten every trace through
    it — no error, just an ancestry that stops early — so pin it here. The
    converse does NOT hold: enrich is crossable without being row-driven."""
    for stage_type in StageType:
        if not is_grain_and_order_preserving(stage_type):
            continue
        if stage_type is StageType.input_data:
            continue  # originates rows; there is no input to cross into
        assert find_positional_cross(stage_type) == (0, 1), stage_type


def test_no_type_names_a_subject_input_it_does_not_have():
    for stage_type in StageType:
        cross = find_positional_cross(stage_type)
        if cross is not None:
            assert 0 <= cross.subject_input < cross.input_count, stage_type
