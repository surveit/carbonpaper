"""count_distinct on real data: one count per distinct value, nulls not counted."""
from __future__ import annotations

import pandas as pd

from conftest import as_inputs, place_stage, rows_of
from app.models import parse_stage
from app.runtime.context import RunContext
from app.runtime.stages.aggregate import handle_aggregate

# `a` repeats a registrant, `b` has nothing but nulls, `c` mixes a value with a null.
FILINGS = pd.DataFrame({
    "firm": ["a", "a", "a", "b", "b", "c", "c"],
    "registrant": ["x", "x", "y", None, None, "z", None],
})
_IN_SCHEMA = {"columns": [{"name": "firm", "type": "str", "nullable": False},
                          {"name": "registrant", "type": "str", "nullable": True}]}


def _counts() -> pd.DataFrame:
    stage = parse_stage({
        "id": "agg", "type": "aggregate", "description": "agg",
        "inputs": [{"id": "filings"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "filings", "columns": _IN_SCHEMA["columns"]}],
            "produces": [{"name": "firm", "type": "str", "nullable": False},
                         {"name": "n_registrants", "type": "int", "nullable": True}]},
        "aggregate": {"group_by": ["firm"], "aggregations": [
            {"output_column": "n_registrants", "formula": "count_distinct",
             "value_column": "registrant"}]},
    })
    ctx = RunContext.for_stages_outside_a_run(run_dir=None)
    return rows_of(handle_aggregate(place_stage(stage), as_inputs({"filings": FILINGS}), ctx))


def _by_firm() -> dict[str, int]:
    out = _counts()
    return dict(zip(out["firm"], out["n_registrants"]))


def test_a_repeated_value_counts_once():
    assert _by_firm()["a"] == 2  # x, x, y


def test_a_null_is_not_one_of_the_distinct_values():
    # `c` filed under one registrant plus a row that names none: one distinct, not two.
    assert _by_firm()["c"] == 1


def test_a_group_of_only_nulls_counts_zero():
    # Not 1 (a null is not a value) and not missing (the group has rows).
    assert _by_firm()["b"] == 0


def test_the_count_is_a_whole_number():
    assert _counts()["n_registrants"].dtype.kind == "i"
