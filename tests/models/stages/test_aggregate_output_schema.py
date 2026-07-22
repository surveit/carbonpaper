"""find_aggregate_output_issues: a declared output_schema must be deliverable
by the aggregate handle — names from group_by + aggregation output columns,
types from the derivation (count->int, mean->float, sum->numeric value type,
min/max/first->value type, list->list[value type])."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import Stage


def _aggregate_stage(*, output_columns, aggregations, edge_schema="default"):
    """One aggregate stage dict grouping facilities by company. `edge_schema`
    'default' declares company:str, revenue:int, region:str on the input edge;
    None omits the edge schema entirely."""
    if edge_schema == "default":
        edge_schema = {
            "columns": [
                {"name": "company", "type": "str"},
                {"name": "revenue", "type": "int"},
                {"name": "region", "type": "str"},
            ],
        }
    inputs = [{"id": "facilities"}]
    if edge_schema is not None:
        inputs = [{"id": "facilities", "schema": edge_schema}]
    return {
        "id": "totals",
        "name": "Company totals",
        "type": "aggregate",
        "inputs": inputs,
        "aggregate": {"group_by": ["company"], "aggregations": aggregations},
        "output_schema": {"columns": output_columns},
    }


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        Stage.model_validate(stage_dict)
    return str(err.value)


def test_declared_column_not_producible_rejected():
    msg = _issues(_aggregate_stage(
        output_columns=[
            {"name": "company", "type": "str"},
            {"name": "bogus", "type": "str"},
        ],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "bogus" in msg and "company" in msg


def test_count_output_declared_non_int_rejected():
    # No edge schema: count's int derivation needs no input types.
    msg = _issues(_aggregate_stage(
        output_columns=[{"name": "n", "type": "str"}],
        aggregations=[{"output_column": "n", "formula": "count"}],
        edge_schema=None,
    ))
    assert "'n'" in msg and "int" in msg


def test_mean_output_declared_non_float_rejected():
    msg = _issues(_aggregate_stage(
        output_columns=[{"name": "avg_revenue", "type": "int"}],
        aggregations=[
            {"output_column": "avg_revenue", "formula": "mean", "value_column": "revenue"},
        ],
    ))
    assert "avg_revenue" in msg and "float" in msg


def test_sum_of_int_declared_int_accepted():
    stage = Stage.model_validate(_aggregate_stage(
        output_columns=[{"name": "total", "type": "int"}],
        aggregations=[
            {"output_column": "total", "formula": "sum", "value_column": "revenue"},
        ],
    ))
    assert stage.id == "totals"


def test_sum_of_int_declared_str_rejected():
    msg = _issues(_aggregate_stage(
        output_columns=[{"name": "total", "type": "str"}],
        aggregations=[
            {"output_column": "total", "formula": "sum", "value_column": "revenue"},
        ],
    ))
    assert "total" in msg and "int" in msg


def test_list_op_declared_list_of_value_type_accepted():
    stage = Stage.model_validate(_aggregate_stage(
        output_columns=[{"name": "regions", "type": "list[str]"}],
        aggregations=[
            {"output_column": "regions", "formula": "list", "value_column": "region"},
        ],
    ))
    assert stage.id == "totals"


def test_list_op_declared_scalar_rejected():
    msg = _issues(_aggregate_stage(
        output_columns=[{"name": "regions", "type": "str"}],
        aggregations=[
            {"output_column": "regions", "formula": "list", "value_column": "region"},
        ],
    ))
    assert "regions" in msg and "list[str]" in msg


def test_group_by_column_type_must_match_edge():
    msg = _issues(_aggregate_stage(
        output_columns=[{"name": "company", "type": "int"}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "company" in msg and "str" in msg


def test_no_edge_schema_still_checks_names():
    # Name feasibility never needs the edge schema; sum's type is unknowable
    # without it, so a "wrong-looking" sum type passes.
    msg = _issues(_aggregate_stage(
        output_columns=[{"name": "bogus", "type": "str"}],
        aggregations=[{"output_column": "n", "formula": "count"}],
        edge_schema=None,
    ))
    assert "bogus" in msg
    stage = Stage.model_validate(_aggregate_stage(
        output_columns=[{"name": "total", "type": "str"}],
        aggregations=[
            {"output_column": "total", "formula": "sum", "value_column": "revenue"},
        ],
        edge_schema=None,
    ))
    assert stage.id == "totals"


def test_no_output_schema_is_fine():
    spec = _aggregate_stage(
        output_columns=[],
        aggregations=[{"output_column": "n", "formula": "count"}],
    )
    del spec["output_schema"]
    assert Stage.model_validate(spec).output_schema is None


def test_valid_aggregate_passes():
    stage = Stage.model_validate(_aggregate_stage(
        output_columns=[
            {"name": "company", "type": "str"},
            {"name": "n", "type": "int"},
            {"name": "avg_revenue", "type": "float"},
        ],
        aggregations=[
            {"output_column": "n", "formula": "count"},
            {"output_column": "avg_revenue", "formula": "mean", "value_column": "revenue"},
        ],
    ))
    assert stage.id == "totals"
