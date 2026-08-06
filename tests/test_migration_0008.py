"""A stage's stored `name` becomes its `description`, and the revision refuses
rather than invents whenever the payload does not determine one."""
from __future__ import annotations

from typing import Any

import pytest

from app.models.stages.stage_base import (
    STAGE_DESCRIPTION_MAX_CHARS,
    STAGE_ID_MAX_CHARS,
)
from tools.stage_description import (
    DescriptionUndeterminable,
    rename_name_to_description,
)


def _stage(**overrides: Any) -> dict[str, Any]:
    return {"id": "load", "name": "Load the roster snapshot", "type": "input_data",
            **overrides}


def test_a_stored_name_becomes_the_description_verbatim():
    stage = _stage()
    assert rename_name_to_description(stage) is True
    assert stage["description"] == "Load the roster snapshot"
    assert "name" not in stage


def test_running_it_twice_changes_nothing_the_second_time():
    """Idempotent, so a store that half-applied the revision can re-run the pass."""
    stage = _stage()
    rename_name_to_description(stage)
    assert rename_name_to_description(stage) is False
    assert stage["description"] == "Load the roster snapshot"


def test_a_stage_carrying_neither_is_refused():
    with pytest.raises(DescriptionUndeterminable, match="no `name` to become"):
        rename_name_to_description({"id": "load", "type": "input_data"})


def test_a_stage_carrying_both_is_refused_rather_than_one_picked():
    stage = _stage(description="Something else")
    with pytest.raises(DescriptionUndeterminable, match="BOTH"):
        rename_name_to_description(stage)


def test_an_empty_name_is_refused_rather_than_stored_as_a_description():
    with pytest.raises(DescriptionUndeterminable, match="empty"):
        rename_name_to_description(_stage(name="   "))


def test_a_name_over_the_ceiling_is_refused_rather_than_truncated():
    """Truncating would publish a sentence no human wrote — the cardinal rule."""
    long_name = "x" * (STAGE_DESCRIPTION_MAX_CHARS + 1)
    with pytest.raises(DescriptionUndeterminable, match="shorten it by hand"):
        rename_name_to_description(_stage(name=long_name))


def test_an_id_over_the_ceiling_is_refused_rather_than_shortened():
    over = "s" * (STAGE_ID_MAX_CHARS + 1)
    with pytest.raises(DescriptionUndeterminable, match="rename the stage by hand"):
        rename_name_to_description(_stage(id=over))
