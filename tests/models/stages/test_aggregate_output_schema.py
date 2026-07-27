from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import AggregateConfig, InputRef, TableSchema
from app.models.stage import Stage
from app.models.stages import find_output_schema_issues


def _aggregate_stage(*, output_columns, aggregations):
    """One aggregate stage dict grouping facilities by company, its input edge
    declaring company:str, revenue:int, region:str."""
    edge_schema = {
        "columns": [
            {"name": "company", "type": "str"},
            {"name": "revenue", "type": "int"},
            {"name": "region", "type": "str"},
        ],
    }
    return {
        "id": "totals",
        "name": "Company totals",
        "type": "aggregate",
        "inputs": [{"id": "facilities", "schema": edge_schema}],
        "aggregate": {"group_by": ["company"], "aggregations": aggregations},
        "output_schema": {"columns": output_columns},
    }


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        Stage.model_validate(stage_dict)
    return str(err.value)


def _issues_without_edge_schema(*, output_columns, aggregations) -> str:
    """The same check on a stage whose input edge declares no schema, so the
    types the derivation would read are unknowable. `Stage._schemas_declared`
    rejects such an input, so the stage is built valid and then rewritten with
    model_copy: find_aggregate_output_issues' no-edge-schema path is reached
    from callers that do not go through a validated Stage."""
    valid = Stage.model_validate(_aggregate_stage(
        output_columns=[{"name": "company", "type": "str"}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    ))
    stripped = valid.model_copy(update={
        "inputs": [InputRef(id="facilities")],
        "output_schema": (
            None if output_columns is None
            else TableSchema.model_validate({"columns": output_columns})
        ),
        "aggregate": AggregateConfig.model_validate(
            {"group_by": ["company"], "aggregations": aggregations}),
    })
    return "; ".join(find_output_schema_issues(stripped))


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
    msg = _issues_without_edge_schema(
        output_columns=[{"name": "n", "type": "str"}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    )
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


def test_sum_of_str_declared_str_accepted():
    # pandas sum of a string column concatenates, so sum over str derives str.
    stage = Stage.model_validate(_aggregate_stage(
        output_columns=[{"name": "all_regions", "type": "str"}],
        aggregations=[
            {"output_column": "all_regions", "formula": "sum", "value_column": "region"},
        ],
    ))
    assert stage.id == "totals"


def test_sum_of_str_declared_int_rejected():
    msg = _issues(_aggregate_stage(
        output_columns=[{"name": "all_regions", "type": "int"}],
        aggregations=[
            {"output_column": "all_regions", "formula": "sum", "value_column": "region"},
        ],
    ))
    assert "all_regions" in msg and "str" in msg


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
    msg = _issues_without_edge_schema(
        output_columns=[{"name": "bogus", "type": "str"}],
        aggregations=[{"output_column": "n", "formula": "count"}],
    )
    assert "bogus" in msg
    assert _issues_without_edge_schema(
        output_columns=[{"name": "total", "type": "str"}],
        aggregations=[
            {"output_column": "total", "formula": "sum", "value_column": "revenue"},
        ],
    ) == ""


def test_no_output_schema_declared_is_nothing_to_check():
    """find_aggregate_output_issues has nothing to check without an
    output_schema. `Stage._schemas_declared` now requires one, so this pins the
    helper's own guard rather than a constructible stage."""
    assert _issues_without_edge_schema(
        output_columns=None,
        aggregations=[{"output_column": "n", "formula": "count"}],
    ) == ""


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
