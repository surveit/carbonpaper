"""Spike (issue #194): the aggregate stage on DuckDB, head to head with pandas.

Every behavioural difference the spike found is pinned here by running the
*production* handler (`app.runtime.stages.aggregate.handle_aggregate`) and the
prototype over the same input and asserting both results. A test that only
checked the prototype would be a claim; running both is the measurement.
"""
from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from app.models import Stage
from app.runtime.stages.aggregate import handle_aggregate
from app.spikes.substrate.duckdb_aggregate import aggregate_sql, run_aggregate_duckdb


def _stage(aggregations: list[dict], group_by: list[str] | None = None) -> Stage:
    return Stage.model_validate({
        "id": "agg",
        "name": "agg",
        "type": "aggregate",
        "inputs": [{"id": "src"}],
        "aggregate": {
            "group_by": ["country"] if group_by is None else group_by,
            "aggregations": aggregations,
        },
    })


def _payments() -> pd.DataFrame:
    return pd.DataFrame({
        "country": ["FR", "FR", "IE"],
        "amount": [2000.0, 5.0, 3.0],
        "name": ["a", "b", None],
    })


# ── the statement itself ─────────────────────────────────────────────────────

def test_the_whole_stage_is_one_statement():
    """The lineage claim: an aggregate stage's meaning is a single readable SQL
    string, not N group-bys plus N-1 merges."""
    sql = aggregate_sql(_stage([
        {"output_column": "big", "formula": "count", "where": "amount > 1000"},
        {"output_column": "total", "formula": "sum", "value_column": "amount"},
    ]).aggregate)
    assert sql == (
        'SELECT "country", count(*) FILTER (WHERE ((amount > 1000))) AS "big", '
        'sum("amount") AS "total"\n'
        "FROM stage_input\n"
        'GROUP BY "country"\n'
        'ORDER BY "country"'
    )


def test_embedded_filter_is_the_rendered_parse_not_the_authored_string():
    """The invariant, carried into the executed statement: what appears in the
    SQL is what `parse_sql_predicate` rendered from the tree it validated."""
    sql = aggregate_sql(_stage([
        {"output_column": "big", "formula": "count", "where": "amount>1000 and country='FR'"},
    ]).aggregate)
    assert "FILTER (WHERE (((amount > 1000) AND (country = 'FR'))))" in sql


def test_a_column_name_that_is_a_keyword_stays_a_name():
    sql = aggregate_sql(_stage(
        [{"output_column": "n", "formula": "count"}], group_by=["order"],
    ).aggregate)
    assert 'GROUP BY "order"' in sql


# ── difference 1: the group set ──────────────────────────────────────────────

def test_a_group_no_filter_admits_survives_on_duckdb_and_vanishes_on_pandas():
    stage = _stage([{"output_column": "big", "formula": "count", "where": "amount > 1000"}])
    frame = _payments()

    assert handle_aggregate(stage, {"src": frame}, None).to_dict("records") == [
        {"country": "FR", "big": 1},
    ]
    assert run_aggregate_duckdb(stage, {"src": frame}).to_dict("records") == [
        {"country": "FR", "big": 1},
        {"country": "IE", "big": 0},
    ]


# ── difference 2: null counts ────────────────────────────────────────────────

def test_pandas_outer_merge_turns_an_int_count_into_a_float_with_nan():
    stage = _stage([
        {"output_column": "big", "formula": "count", "where": "amount > 1000"},
        {"output_column": "total", "formula": "sum", "value_column": "amount"},
    ])
    frame = _payments()

    from_pandas = handle_aggregate(stage, {"src": frame}, None)
    assert str(from_pandas["big"].dtype) == "float64"
    assert from_pandas.loc[from_pandas["country"] == "IE", "big"].isna().all()

    from_duckdb = run_aggregate_duckdb(stage, {"src": frame})
    assert str(from_duckdb["big"].dtype) == "int64[pyarrow]"
    assert from_duckdb.loc[from_duckdb["country"] == "IE", "big"].tolist() == [0]


# ── difference 3: order-sensitive formulas ───────────────────────────────────

def test_first_matches_pandas_first_non_null_in_input_order():
    stage = _stage([{"output_column": "who", "formula": "first", "value_column": "name"}])
    frame = pd.DataFrame({"country": ["IE", "IE", "IE"], "name": [None, "b", "c"]})

    assert handle_aggregate(stage, {"src": frame}, None).to_dict("records") == [
        {"country": "IE", "who": "b"},
    ]
    assert run_aggregate_duckdb(stage, {"src": frame}).to_dict("records") == [
        {"country": "IE", "who": "b"},
    ]


def test_first_is_ordered_by_input_row_not_by_scan_order():
    """`first` carries an explicit ORDER BY over a row-number column attached to
    the input, so reversing the input reverses the answer — which is what makes
    the result a fact about the data rather than about the scan."""
    stage = _stage([{"output_column": "who", "formula": "first", "value_column": "name"}])
    frame = pd.DataFrame({"country": ["IE", "IE"], "name": ["b", "c"]})

    assert run_aggregate_duckdb(stage, {"src": frame})["who"].tolist() == ["b"]
    reversed_frame = frame.iloc[::-1].reset_index(drop=True)
    assert run_aggregate_duckdb(stage, {"src": reversed_frame})["who"].tolist() == ["c"]


def test_list_carries_real_nulls_where_pandas_carries_nan():
    """The issue's `list[str]` bug seen from the producing side: pandas puts
    `float('nan')` inside the list, so the column is not `list[str]` at all."""
    stage = _stage([{"output_column": "names", "formula": "list", "value_column": "name"}])
    frame = _payments()

    from_pandas = handle_aggregate(stage, {"src": frame}, None)
    ie_pandas = from_pandas.loc[from_pandas["country"] == "IE", "names"].iloc[0]
    assert len(ie_pandas) == 1 and pd.isna(ie_pandas[0])

    from_duckdb = run_aggregate_duckdb(stage, {"src": frame})
    ie_duckdb = from_duckdb.loc[from_duckdb["country"] == "IE", "names"].iloc[0]
    assert list(ie_duckdb) == [None]
    assert str(from_duckdb["names"].dtype).startswith("list<")


# ── difference 4: group order ────────────────────────────────────────────────

def test_group_order_is_stated_so_the_result_is_reproducible():
    stage = _stage([{"output_column": "n", "formula": "count"}])
    frame = pd.DataFrame({"country": ["IE", "FR", "DE", "FR"]})
    assert run_aggregate_duckdb(stage, {"src": frame})["country"].tolist() == ["DE", "FR", "IE"]


# ── difference 5: sum over a str column ──────────────────────────────────────

def test_sum_over_a_string_column_concatenates_on_pandas_and_is_refused_by_duckdb():
    stage = _stage([{"output_column": "cat", "formula": "sum", "value_column": "name"}])
    frame = pd.DataFrame({"country": ["FR", "FR"], "name": ["a", "b"]})

    assert handle_aggregate(stage, {"src": frame}, None).to_dict("records") == [
        {"country": "FR", "cat": "ab"},
    ]
    with pytest.raises(duckdb.BinderException):
        run_aggregate_duckdb(stage, {"src": frame})


# ── nulls in the grouping key, and the empty case ────────────────────────────

def test_a_null_group_key_is_its_own_group_on_both_substrates():
    stage = _stage([{"output_column": "n", "formula": "count"}])
    frame = pd.DataFrame({"country": ["FR", None, None]})

    from_duckdb = run_aggregate_duckdb(stage, {"src": frame})
    assert from_duckdb["n"].tolist() == [1, 2]
    assert from_duckdb["country"].isna().tolist() == [False, True]


def test_empty_input_keeps_its_columns_and_their_types():
    stage = _stage([
        {"output_column": "big", "formula": "count", "where": "amount > 1000"},
        {"output_column": "total", "formula": "sum", "value_column": "amount"},
    ])
    result = run_aggregate_duckdb(stage, {"src": _payments().head(0)})
    assert list(result.columns) == ["country", "big", "total"]
    assert str(result["big"].dtype) == "int64[pyarrow]"
    assert str(result["total"].dtype) == "double[pyarrow]"
    assert len(result) == 0


def test_no_group_by_aggregates_the_whole_frame():
    stage = _stage([{"output_column": "n", "formula": "count"}], group_by=[])
    assert run_aggregate_duckdb(stage, {"src": _payments()}).to_dict("records") == [{"n": 3}]


def test_a_non_aggregate_stage_is_refused():
    stage = Stage.model_validate({
        "id": "t", "name": "t", "type": "python_row_function",
        "inputs": [{"id": "src"}],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
    })
    with pytest.raises(ValueError, match="not an aggregate stage"):
        run_aggregate_duckdb(stage, {"src": _payments()})
