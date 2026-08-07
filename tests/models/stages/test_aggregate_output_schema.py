"""The aggregate signature's `produces` against what the config computes:
producible by name, at the type each formula fixes."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import parse_stage

_EDGE_COLUMNS = [
    {"name": "company", "type": "str", "nullable": True},
    {"name": "revenue", "type": "int", "nullable": True},
    {"name": "region", "type": "str", "nullable": True},
]


def _aggregate_stage(*, produces, aggregations):
    """One aggregate stage dict grouping facilities by company, its input edge
    declaring company:str, revenue:int, region:str. Reads are exactly what the
    config consumes, as the signature check requires."""
    consumed = {"company"} | {
        op["value_column"] for op in aggregations if op.get("value_column")
    }
    return {
        "id": "totals",
        "name": "Company totals",
        "type": "aggregate",
        "inputs": [{"id": "facilities", "schema": {"columns": _EDGE_COLUMNS}}],
        "aggregate": {"group_by": ["company"], "aggregations": aggregations},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "facilities",
                       "columns": [c for c in _EDGE_COLUMNS if c["name"] in consumed]}],
            "produces": produces,
        },
    }


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        parse_stage(stage_dict)
    return str(err.value)


def test_produced_column_not_producible_rejected():
    msg = _issues(_aggregate_stage(
        produces=[
            {"name": "company", "type": "str", "nullable": True},
            {"name": "n", "type": "int", "nullable": True},
            {"name": "bogus", "type": "str", "nullable": True},
        ],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "bogus" in msg and "company" in msg


def test_count_output_declared_non_int_rejected():
    # count gives int regardless of the input types.
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "n", "type": "str", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "'n'" in msg and "int" in msg


def test_mean_output_declared_non_float_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "avg_revenue", "type": "int", "nullable": True}],
        aggregations=[
            {"output_column": "avg_revenue", "formula": "mean", "value_column": "revenue"},
        ],
    ))
    assert "avg_revenue" in msg and "float" in msg


def test_sum_of_int_declared_int_accepted():
    stage = parse_stage(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "total", "type": "int", "nullable": True}],
        aggregations=[
            {"output_column": "total", "formula": "sum", "value_column": "revenue"},
        ],
    ))
    assert stage.id == "totals"


def test_sum_of_int_declared_str_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "total", "type": "str", "nullable": True}],
        aggregations=[
            {"output_column": "total", "formula": "sum", "value_column": "revenue"},
        ],
    ))
    assert "total" in msg and "int" in msg


def test_sum_of_str_declared_str_accepted():
    # pandas sum of a string column concatenates, so sum over str gives str.
    stage = parse_stage(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "all_regions", "type": "str", "nullable": True}],
        aggregations=[
            {"output_column": "all_regions", "formula": "sum", "value_column": "region"},
        ],
    ))
    assert stage.id == "totals"


def test_sum_of_str_declared_int_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "all_regions", "type": "int", "nullable": True}],
        aggregations=[
            {"output_column": "all_regions", "formula": "sum", "value_column": "region"},
        ],
    ))
    assert "all_regions" in msg and "str" in msg


def test_list_op_declared_list_of_value_type_accepted():
    stage = parse_stage(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "regions", "type": "list[str]", "nullable": True}],
        aggregations=[
            {"output_column": "regions", "formula": "list", "value_column": "region"},
        ],
    ))
    assert stage.id == "totals"


def test_list_op_declared_scalar_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "regions", "type": "str", "nullable": True}],
        aggregations=[
            {"output_column": "regions", "formula": "list", "value_column": "region"},
        ],
    ))
    assert "regions" in msg and "list[str]" in msg


def test_group_by_column_type_must_match_edge():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "int", "nullable": True},
                  {"name": "n", "type": "int", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "company" in msg and "str" in msg


def test_an_emitted_column_omitted_from_produces_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "emits `n`" in msg and "omits it" in msg


def test_valid_aggregate_passes():
    stage = parse_stage(_aggregate_stage(
        produces=[
            {"name": "company", "type": "str", "nullable": True},
            {"name": "n", "type": "int", "nullable": True},
            {"name": "avg_revenue", "type": "float", "nullable": True},
        ],
        aggregations=[
            {"output_column": "n", "formula": "count"},
            {"output_column": "avg_revenue", "formula": "mean", "value_column": "revenue"},
        ],
    ))
    assert stage.id == "totals"
