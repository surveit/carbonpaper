from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import parse_stage


def _aggregate_stage(*, produces, aggregations):
    edge_schema = {
        "columns": [
            {"name": "company", "type": "str", "nullable": True},
            {"name": "revenue", "type": "int", "nullable": True},
            {"name": "region", "type": "str", "nullable": True},
        ],
    }
    return {
        "id": "totals",
        "description": "Company totals",
        "type": "aggregate",
        "inputs": [{"id": "facilities", "schema": edge_schema}],
        "aggregate": {"group_by": ["company"], "aggregations": aggregations},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "facilities", "columns": _reads_for(aggregations, edge_schema)}],
            "produces": produces,
        },
    }


def _reads_for(aggregations, edge_schema):
    consumed = ["company", *(op["value_column"] for op in aggregations
                             if op.get("value_column"))]
    by_name = {c["name"]: c for c in edge_schema["columns"]}
    return [by_name[name] for name in dict.fromkeys(consumed) if name in by_name]


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        parse_stage(stage_dict)
    return str(err.value)


def test_declared_column_not_producible_rejected():
    msg = _issues(_aggregate_stage(
        produces=[
            {"name": "company", "type": "str", "nullable": True},
            {"name": "bogus", "type": "str", "nullable": True},
        ],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "bogus" in msg and "company" in msg


def test_count_output_declared_non_int_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "n", "type": "str", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "'n'" in msg and "int" in msg


def test_mean_output_declared_non_float_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "avg_revenue", "type": "int", "nullable": True}],
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
        produces=[{"name": "total", "type": "str", "nullable": True}],
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
        produces=[{"name": "all_regions", "type": "int", "nullable": True}],
        aggregations=[
            {"output_column": "all_regions", "formula": "sum", "value_column": "region"},
        ],
    ))
    assert "all_regions" in msg and "str" in msg


def test_count_distinct_without_a_value_column_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                        {"name": "n_regions", "type": "int", "nullable": True}],
        aggregations=[{"output_column": "n_regions", "formula": "count_distinct"}],
    ))
    assert "n_regions" in msg and "value_column" in msg


def test_count_distinct_output_declared_int_accepted():
    stage = parse_stage(_aggregate_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                        {"name": "n_regions", "type": "int", "nullable": True}],
        aggregations=[
            {"output_column": "n_regions", "formula": "count_distinct",
             "value_column": "region"},
        ],
    ))
    assert stage.id == "totals"


def test_count_distinct_output_declared_as_the_value_type_rejected():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "n_regions", "type": "str", "nullable": True}],
        aggregations=[
            {"output_column": "n_regions", "formula": "count_distinct",
             "value_column": "region"},
        ],
    ))
    assert "n_regions" in msg and "int" in msg


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
        produces=[{"name": "regions", "type": "str", "nullable": True}],
        aggregations=[
            {"output_column": "regions", "formula": "list", "value_column": "region"},
        ],
    ))
    assert "regions" in msg and "list[str]" in msg


def test_group_by_column_type_must_match_edge():
    msg = _issues(_aggregate_stage(
        produces=[{"name": "company", "type": "int", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "company" in msg and "str" in msg


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


# ---- group_by: [] — the whole frame as one group ---------------------------
# The produced schema is then the aggregation outputs ALONE: there is no group
# column to carry an edge type through, so a declared one has nothing to match.

def _whole_frame_stage(*, produces, aggregations):
    stage = _aggregate_stage(produces=produces, aggregations=aggregations)
    stage["aggregate"]["group_by"] = []
    # `company` was consumed only as the group key, so dropping group_by drops
    # it from reads — and a bare `count` then consumes nothing at all, which is
    # `reads: []`, not an entry with no columns (InputReads needs one).
    columns = [c for c in stage["signature"]["reads"][0]["columns"]
               if c["name"] != "company"]
    stage["signature"]["reads"] = (
        [{"input": "facilities", "columns": columns}] if columns else []
    )
    return stage


def test_whole_frame_producing_the_aggregations_alone_accepted():
    stage = parse_stage(_whole_frame_stage(
        produces=[{"name": "n", "type": "int", "nullable": True},
                  {"name": "total", "type": "int", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"},
                      {"output_column": "total", "formula": "sum",
                       "value_column": "revenue"}],
    ))
    assert stage.aggregate.group_by == []


def test_whole_frame_declaring_a_group_column_rejected():
    msg = _issues(_whole_frame_stage(
        produces=[{"name": "company", "type": "str", "nullable": True},
                  {"name": "n", "type": "int", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "company" in msg


def test_whole_frame_output_types_still_follow_the_formula():
    msg = _issues(_whole_frame_stage(
        produces=[{"name": "n", "type": "str", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert "'n'" in msg and "int" in msg


def test_whole_frame_reading_a_column_the_config_never_consumes_rejected():
    stage = _whole_frame_stage(
        produces=[{"name": "n", "type": "int", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    )
    stage["signature"]["reads"] = [{
        "input": "facilities",
        "columns": [{"name": "region", "type": "str", "nullable": True}],
    }]
    assert "region" in _issues(stage)


def test_compute_aggregate_output_types_emits_the_aggregations_alone():
    from app.models.stages.aggregate import compute_aggregate_output_types

    stage = parse_stage(_whole_frame_stage(
        produces=[{"name": "n", "type": "int", "nullable": True},
                  {"name": "regions", "type": "list[str]", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"},
                      {"output_column": "regions", "formula": "list",
                       "value_column": "region"}],
    ))
    computed = compute_aggregate_output_types(
        stage.aggregate, stage.inputs[0].table_schema)

    assert computed == {"n": "int", "regions": "list[str]"}


def test_whole_frame_counting_rows_consumes_no_column_at_all():
    stage = parse_stage(_whole_frame_stage(
        produces=[{"name": "n", "type": "int", "nullable": True}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    assert stage.signature.reads == []
