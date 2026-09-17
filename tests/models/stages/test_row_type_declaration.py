"""A stage whose rows are a new kind of thing names the word; the rest read their input's."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import RowType, StageDraft, StageType, parse_stage
from app.models.row_types import NO_KIND_ROW_TYPE_ID
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
    StageType.report: True,
    StageType.enrich: False,
    StageType.filter_rows: False,
    StageType.human_review_queue: False,
    StageType.llm_transform: False,
    StageType.python_row_function: False,
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


def _report_stage(**extra):
    return {
        "id": "write_the_cards", "type": "report", "description": "Write one card per visit",
        "inputs": [{"id": "src"}],
        "signature": {"form": "replaces", "reads": reads_of("src", _VISIT_COLUMNS)},
        "report": {"format": "evidence_cards"},
        "function": {"kind": "inline", "summary": "Writes one card per row.",
                     "code": "def transform(df, output_dir, citation_provider):\n    return df"},
        **extra,
    }


def _frame_function_stage(**extra):
    return {
        "id": "pivot_visits", "type": "python_frame_function",
        "description": "Pivot visits onto one row per facility",
        "inputs": [{"id": "src"}],
        "signature": {"form": "replaces", "reads": reads_of("src", _VISIT_COLUMNS),
                      "produces": [dict(_VISIT_COLUMNS[0])]},
        "function": {"kind": "inline", "summary": "Pivots the frame.",
                     "code": "def transform(frame):\n    return frame",
                     "corner_cases": [{"case": "no rows", "expected": "no rows"}]},
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
    assert stage.resolve_own_row_type_id() == "facility"


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


def test_an_ungrouped_aggregate_answers_no_kind_with_the_field_left_out():
    stage = parse_stage(_aggregate_stage(group_by=[]))
    assert stage.row_type_id is None
    assert stage.resolve_own_row_type_id() == NO_KIND_ROW_TYPE_ID


def test_an_ungrouped_aggregate_naming_a_word_is_refused():
    with pytest.raises(ValidationError, match="a figure ABOUT the whole input population"):
        parse_stage(_aggregate_stage(group_by=[], row_type_id="facility"))


def test_a_report_answers_no_kind_with_the_field_left_out():
    stage = parse_stage(_report_stage())
    assert stage.row_type_id is None
    assert stage.resolve_own_row_type_id() == NO_KIND_ROW_TYPE_ID


def test_a_report_naming_a_word_is_refused():
    with pytest.raises(ValidationError, match="report emits files, not rows"):
        parse_stage(_report_stage(row_type_id="facility"))


@pytest.mark.parametrize("spec", [
    pytest.param(_aggregate_stage(group_by=["facility_id"], row_type_id=NO_KIND_ROW_TYPE_ID),
                 id="grouped_aggregate"),
    pytest.param(_aggregate_stage(group_by=[], row_type_id=NO_KIND_ROW_TYPE_ID),
                 id="ungrouped_aggregate"),
    pytest.param(_report_stage(row_type_id=NO_KIND_ROW_TYPE_ID), id="report"),
    pytest.param(_frame_function_stage(row_type_id=NO_KIND_ROW_TYPE_ID), id="frame_function"),
    pytest.param(_dedupe_stage(row_type_id=NO_KIND_ROW_TYPE_ID), id="dedupe"),
    pytest.param(_filter_stage(row_type_id=NO_KIND_ROW_TYPE_ID), id="filter_rows"),
])
def test_no_stage_may_be_told_the_reserved_word(spec):
    with pytest.raises(ValidationError, match="`no_kind` is never written"):
        parse_stage(spec)


def test_a_frame_function_names_a_word_like_any_other_reshaping_type():
    stage = parse_stage(_frame_function_stage(row_type_id="facility"))
    assert stage.resolve_own_row_type_id() == "facility"


def test_no_row_type_may_be_declared_under_the_reserved_word():
    with pytest.raises(ValidationError, match="reserved for the stages"):
        RowType(id=NO_KIND_ROW_TYPE_ID, title="No kind", definition="Not a kind of thing.")


def test_a_draft_carries_the_word_through_to_the_stage_spec():
    draft = StageDraft.model_validate(_dedupe_stage(row_type_id="facility"))
    assert draft.row_type_id == "facility"
    assert draft.to_stage_spec()["row_type_id"] == "facility"


def test_a_draft_stores_the_word_an_inheriting_type_cannot_keep():
    draft = StageDraft.model_validate(_filter_stage(row_type_id="facility"))
    with pytest.raises(ValidationError, match="`filter_rows` output rows are the input's kind"):
        parse_stage(draft.to_stage_spec())
