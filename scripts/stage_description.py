"""The `name` → `description` rename, shared by alembic 0008 and the compiled-file
migration so the store and the working copies move by identical rules.
"""
from __future__ import annotations

from typing import Any

from app.models.stages.stage_base import (
    STAGE_DESCRIPTION_MAX_CHARS,
    STAGE_ID_MAX_CHARS,
)


class DescriptionUndeterminable(RuntimeError):
    """A stage this rename cannot move without inventing what it says."""


def rename_name_to_description(stage: dict[str, Any]) -> bool:
    """Move one stage spec's `name` to `description`; True if the payload changed.

    Idempotent: a spec already renamed returns False untouched. Raises rather than
    shorten, drop, or synthesize anything a human has to decide."""
    stage_id = str(stage.get("id") or "?")
    _refuse_oversized_id(stage_id)
    name = stage.get("name")
    if name is None:
        if "description" in stage:
            return False
        raise DescriptionUndeterminable(
            f"{stage_id}: no `name` to become its description, and no description"
        )
    if "description" in stage:
        raise DescriptionUndeterminable(
            f"{stage_id}: carries BOTH `name` and `description` — only a human knows "
            f"which one survives"
        )
    if not isinstance(name, str) or not name.strip():
        raise DescriptionUndeterminable(f"{stage_id}: its `name` is empty ({name!r})")
    if len(name) > STAGE_DESCRIPTION_MAX_CHARS:
        raise DescriptionUndeterminable(
            f"{stage_id}: its name is {len(name)} characters, over the "
            f"{STAGE_DESCRIPTION_MAX_CHARS}-character description limit — shorten it by "
            f"hand rather than have this truncate it"
        )
    del stage["name"]
    stage["description"] = name
    return True


def _refuse_oversized_id(stage_id: str) -> None:
    if len(stage_id) > STAGE_ID_MAX_CHARS:
        raise DescriptionUndeterminable(
            f"{stage_id}: its id is {len(stage_id)} characters, over the new "
            f"{STAGE_ID_MAX_CHARS}-character limit — rename the stage by hand"
        )
