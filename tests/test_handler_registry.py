"""The registry's handler shapes are the runtime half of the model's
grain-and-order declaration; the registry import holds them equal, and these
tests pin both the equality and the total coverage of stage types."""
from __future__ import annotations

from app.models.stage import GRAIN_AND_ORDER_PRESERVING_TYPES, StageType
from app.runtime.stages import HANDLERS


def test_registry_shapes_match_model_declaration():
    derived = frozenset(
        stage_type for stage_type, handler in HANDLERS.items()
        if handler.is_grain_and_order_preserving
    )
    assert derived == GRAIN_AND_ORDER_PRESERVING_TYPES


def test_every_stage_type_has_a_handler():
    assert set(HANDLERS) == set(StageType)
