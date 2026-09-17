"""A stage whose output rows are a new kind of thing says which; the rest inherit."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import StageDraft, StageType, parse_stage
from app.models.stages.stage_base import declares_its_own_row_type
from conftest import reads_of

_VISIT_COLUMNS = [
    {"name": "facility_id", "type": "str", "nullable": True},
    {"name": "fine", "type": "int", "nullable": True},
]

# Exhaustive over StageType, so a new stage type fails here until it is classified.
_DECLARES_ITS_OWN_ROW_TYPE = {
    StageType.input_data: True,
    StageType.aggregate: True,
    StageType.dedupe: True,
    StageType.explode: True,
    StageType.expand: True,
    StageType.python_frame_function: True,
    StageType.enrich: False,
    StageType.filter_rows: False,
    StageType.human_review_queue: False,
    StageType.llm_transform: False,
    StageType.python_row_function: False,
    StageType.report: False,
    StageType.sort_rank: False,
    StageType.starlark_filter_rows: False,
    StageType.starlark_row_function: False,
    StageType.union: False,
}


def _dedupe_stage(**extra):
    return {
        "id": "one_per_facility", "type": "dedupe", "description": "Collapse visits",
        "inputs": [{"id": "src"}],
        "signature": {"form": "extends", "reads": reads_of("src", _VISIT_COLUMNS)},
        "dedupe": {"keys": ["facility_id"], "keep": "agree"},
        **extra,
    }


def _filter_stage(**extra):
    return {
        "id": "fined_only", "type": "filter_rows", "description": "Keep fined visits",
        "inputs": [{"id": "src"}],
        "signature": {"form": "extends", "reads": reads_of("src", _VISIT_COLUMNS)},
        "filter": {"code": "def should_include(row): return row['fine'] > 0"},
        **extra,
    }


def _aggregate_stage(*, group_by, **extra):
    produces = [dict(_VISIT_COLUMNS[0])] if group_by else []
    return {
        "id": "totals", "type": "aggregate", "description": "Total the fines",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "replaces",
            "reads": reads_of("src", [c for c in _VISIT_COLUMNS
                                      if c["name"] in {*group_by, "fine"}]),
            "produces": produces + [{"name": "fines", "type": "int", "nullable": True}],
        },
        "aggregate": {
            "group_by": list(group_by),
            "aggregations": [
                {"output_column": "fines", "formula": "sum", "value_column": "fine"}
            ],
        },
        **extra,
    }


def test_every_stage_type_is_classified():
    assert set(_DECLARES_ITS_OWN_ROW_TYPE) == set(StageType)


@pytest.mark.parametrize(
    "stage_type,declares",
    sorted((t.value, declares) for t, declares in _DECLARES_ITS_OWN_ROW_TYPE.items()),
)
def test_a_stage_type_declares_or_inherits(stage_type, declares):
    assert declares_its_own_row_type(StageType(stage_type)) is declares


def test_a_declaring_stage_carries_the_word():
    stage = parse_stage(_dedupe_stage(row_type_id="facility"))
    assert stage.declares_its_own_row_type is True
    assert stage.row_type_id == "facility"


def test_an_inheriting_stage_leaves_the_field_absent():
    stage = parse_stage(_filter_stage())
    assert stage.declares_its_own_row_type is False
    assert stage.row_type_id is None
    assert "row_type_id" not in stage.model_dump(exclude_none=True)


def test_an_inheriting_stage_naming_a_row_type_is_refused():
    with pytest.raises(ValidationError, match="`filter_rows` output rows are the input's kind"):
        parse_stage(_filter_stage(row_type_id="facility"))


def test_a_grouped_aggregate_declares_its_groups():
    stage = parse_stage(_aggregate_stage(group_by=["facility_id"], row_type_id="facility"))
    assert stage.declares_its_own_row_type is True
    assert stage.row_type_id == "facility"


def test_an_ungrouped_aggregate_reads_through_to_its_input():
    assert parse_stage(_aggregate_stage(group_by=[])).declares_its_own_row_type is False


def test_an_ungrouped_aggregate_naming_a_row_type_is_refused():
    with pytest.raises(ValidationError, match="`aggregate` output rows are the input's kind"):
        parse_stage(_aggregate_stage(group_by=[], row_type_id="facility"))


def test_a_draft_carries_the_word_through_to_the_stage_spec():
    draft = StageDraft.model_validate(_dedupe_stage(row_type_id="facility"))
    assert draft.row_type_id == "facility"
    assert draft.to_stage_spec()["row_type_id"] == "facility"


def test_a_draft_stores_the_word_an_inheriting_type_cannot_keep():
    draft = StageDraft.model_validate(_filter_stage(row_type_id="facility"))
    with pytest.raises(ValidationError, match="`filter_rows` output rows are the input's kind"):
        parse_stage(draft.to_stage_spec())
