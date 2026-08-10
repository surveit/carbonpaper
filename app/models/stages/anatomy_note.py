"""The stage anatomy every authoring prompt states ONCE, above its type catalog:
what every stage declares, and the row-grain table rendered from
_GRAIN_AND_ORDER_PRESERVING_TYPES so the prose cannot contradict the registry the
runtime is held to.
"""
from __future__ import annotations

from app.models.stages.node_types import AUTHORABLE_TYPES
from app.models.stages.stage_base import StageType, is_grain_and_order_preserving

_WHAT_EVERY_STAGE_DECLARES = """\
Every stage declares: `id` (its one name), `description`, `inputs` (the stage ids it
reads, each with the schema it expects), `signature`, and exactly one config block
named by its type. An input's declared schema must be a subset of what that upstream
stage's signature promises."""

_NULLS = """\
Absence is null, never a filled-in value. An unmatched join lands nulls; an aggregate
over no rows reports every figure null rather than 0, which would claim something was
measured."""


def render_stage_anatomy() -> str:
    return "\n\n".join([
        _WHAT_EVERY_STAGE_DECLARES,
        _render_grain_table(),
        _NULLS,
    ])


def _render_grain_table() -> str:
    """One line per type, so no type's own note has to restate its row grain."""
    one_to_one = sorted(t for t in _catalog_types() if is_grain_and_order_preserving(t))
    reshaping = sorted(t for t in _catalog_types() if not is_grain_and_order_preserving(t))
    return "\n".join([
        "Row grain — whether one input row becomes exactly one output row, in order. "
        "Fixed by type.",
        f"  1:1, order preserved: {_names(one_to_one)}",
        f"  may add, drop or reorder rows: {_names(reshaping)}",
        "A stage that reshapes breaks row-position provenance: a figure computed in "
        "one cannot be traced to the rows behind it.",
    ])


def _catalog_types() -> list[StageType]:
    return [StageType(name) for name in AUTHORABLE_TYPES]


def _names(types: list[StageType]) -> str:
    return ", ".join(t.value for t in types)
