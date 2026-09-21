"""The authored phrase for what a step keeps, wherever a step decides which rows do."""

from __future__ import annotations

from typing import Optional

from app.models.stages.stage_base import AbstractStage

# Every config block that carries a `predicate`: the blocks whose step decides.
PREDICATE_BLOCKS = ("filter", "starlark_filter", "queue")

# The migration filler, which every reader treats as unwritten. Alembic 0022.
PREDICATE_NOT_WRITTEN = "pass this step's test"


def read_the_predicate(authored: AbstractStage) -> Optional[str]:
    """None for the migration filler: nobody wrote it, so nothing may quote it."""
    for holder in PREDICATE_BLOCKS:
        block = getattr(authored, holder, None)
        written = getattr(block, "predicate", None) if block is not None else None
        if written and written != PREDICATE_NOT_WRITTEN:
            return str(written)
    return None


def decides_which_rows_are_kept(authored: AbstractStage) -> bool:
    return any(getattr(authored, holder, None) is not None
               for holder in PREDICATE_BLOCKS)
