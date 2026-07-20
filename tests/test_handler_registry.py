"""Each stage type's registered handler SHAPE must agree with the core
grain-and-order fact (a preserving type is registered as a row-driven shape, a
non-preserving one as a FrameHandler). The registry import already enforces this
via validate_registry_matches_model; these tests pin the equality and the total
coverage of stage types as named, discoverable checks."""
from __future__ import annotations

from app.core.models.stage import StageType, is_grain_and_order_preserving
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import RowMapHandler, SourceHandler


def test_registry_shape_matches_the_model_for_every_type():
    for stage_type, handler in HANDLERS.items():
        shape_preserves = isinstance(handler, (RowMapHandler, SourceHandler))
        assert shape_preserves == is_grain_and_order_preserving(stage_type), stage_type


def test_every_stage_type_has_a_handler():
    assert set(HANDLERS) == set(StageType)
