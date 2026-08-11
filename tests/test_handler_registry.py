"""Each stage type's registered handler must report the same grain-and-order
preservation the core model declares for that type. The registry import already
enforces this via validate_registry_matches_model; these tests pin the equality
of `handler.preserves_grain_and_order` with `is_grain_and_order_preserving`, and
the total coverage of stage types, as named, discoverable checks."""
from __future__ import annotations

import pytest

from app.models.stage import (
    StageType,
    is_grain_and_order_preserving,
    max_declared_inputs,
)
from app.runtime.stages import HANDLERS, RowMapTransformHandler, validate_registry_matches_model


def test_human_review_queue_maps_one_row_at_a_time_so_its_shared_counters_stay_correct():
    """Its mapper's `+=` on one shared QueueStats dict is non-atomic: >1 silently under-counts."""
    handler = HANDLERS[StageType.human_review_queue]
    assert isinstance(handler, RowMapTransformHandler)
    assert handler.parallelism == 1


def test_registry_shape_matches_the_model_for_every_type():
    for stage_type, handler in HANDLERS.items():
        assert handler.preserves_grain_and_order == is_grain_and_order_preserving(stage_type), stage_type


def test_every_stage_type_has_a_handler():
    assert set(HANDLERS) == set(StageType)


def test_every_row_mapped_type_caps_its_inputs_at_one():
    """A row-mapped handler maps ONE frame's rows; a second input would name no rows."""
    for stage_type, handler in HANDLERS.items():
        if isinstance(handler, RowMapTransformHandler):
            assert max_declared_inputs(stage_type) == 1, stage_type


def test_a_multi_input_type_cannot_be_row_mapped():
    """Was a per-execution ValueError in the driver; the registry refuses it earlier now."""
    row_mapped = HANDLERS[StageType.python_row_function]
    with pytest.raises(RuntimeError):
        validate_registry_matches_model({StageType.python_frame_function: row_mapped})


def test_a_row_mapped_registration_over_an_uncapped_type_is_refused_at_import():
    # input_data is the one grain-preserving type with no cap, so it reaches the arity check.
    row_mapped = HANDLERS[StageType.python_row_function]
    with pytest.raises(RuntimeError, match="maps one frame's rows"):
        validate_registry_matches_model({StageType.input_data: row_mapped})
