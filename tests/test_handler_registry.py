"""Each stage type's registered handler must report the same grain-and-order
preservation the core model declares for that type. The registry import already
enforces this via validate_registry_matches_model; these tests pin the equality
of `handler.preserves_grain_and_order` with `is_grain_and_order_preserving`, and
the total coverage of stage types, as named, discoverable checks."""
from __future__ import annotations

from app.models.stage import StageType, is_grain_and_order_preserving
from app.runtime.stages import HANDLERS


def test_registry_shape_matches_the_model_for_every_type():
    for stage_type, handler in HANDLERS.items():
        assert handler.preserves_grain_and_order == is_grain_and_order_preserving(stage_type), stage_type


def test_every_stage_type_has_a_handler():
    assert set(HANDLERS) == set(StageType)
