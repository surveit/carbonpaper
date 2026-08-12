from __future__ import annotations

from app.models import validate_workflow_draft
from conftest import source_stage


def _aggregate_stage(*, group_by, edge_columns, value_column=None, where=None, formula="count",
                     output_n_type="int"):
    aggregation = {"output_column": "n", "formula": formula}
    if value_column is not None:
        aggregation["value_column"] = value_column
    if where is not None:
        aggregation["where"] = where
    # `produces` names each group_by column under its own name — the aggregate
    # handle emits them that way — plus the aggregation's output column, whose
    # declared type must match the formula's computed type: count gives int; sum
    # over these all-str edge columns gives str (concatenation), per
    # compute_aggregate_output_types.
    #
    # These tests vary group_by and value_column to exercise the CONFIG checks,
    # so `reads` is computed from that config: a pinned read set would fail its
    # own cross-check first and mask them.
    edge = {c: {"name": c, "type": "str", "nullable": False} for c in edge_columns}
    consumed = [name for name in dict.fromkeys([*group_by, value_column])
                if name in edge]
    return {
        "id": "agg", "type": "aggregate", "description": "agg",
        "inputs": [{"id": "src"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "src", "columns": [edge[n] for n in consumed]}]
                     if consumed else [],
            "produces": [{"name": group_by[0], "type": "str", "nullable": False},
                         {"name": "n", "type": output_n_type, "nullable": False}],
        },
        "aggregate": {"group_by": group_by, "aggregations": [aggregation]},
    }


def _issues(**kwargs) -> str:
    return "; ".join(validate_workflow_draft([
        source_stage("src", [
            {"name": c, "type": "str", "nullable": False} for c in kwargs["edge_columns"]
        ]),
        _aggregate_stage(**kwargs),
    ]))


def test_group_by_missing_column_rejected():
    assert _issues(group_by=["nope"], edge_columns=["a"])


def test_group_by_present_ok():
    assert _issues(group_by=["a"], edge_columns=["a"]) == ""


def test_value_column_missing_rejected():
    assert _issues(group_by=["a"], edge_columns=["a"], value_column="missing", formula="sum")


def test_value_column_present_ok():
    # sum over a str edge column gives str
    assert _issues(group_by=["a"], edge_columns=["a", "n"], value_column="n",
                   formula="sum", output_n_type="str") == ""


def test_where_missing_column_rejected():
    assert _issues(group_by=["a"], edge_columns=["a"], where="ghost_col > 0")


def test_where_valid_column_ok():
    assert _issues(group_by=["a"], edge_columns=["a"], where="a IS NOT NULL") == ""


def test_where_unparseable_predicate_rejected():
    assert _issues(group_by=["a"], edge_columns=["a"], where="`weird name` == 1")



def test_column_declared_only_on_a_sibling_producer_is_not_enough():
    assert _issues(group_by=["sector"], edge_columns=["a"])
