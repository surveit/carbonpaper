"""A `sum` over a float column is the exact decimal sum, grouped or over the whole frame."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from conftest import as_inputs, place_stage, rows_of

from app.core.figure_text import render_figure
from app.models import parse_stage
from app.runtime.context import RunContext
from app.runtime.stages.aggregate import handle_aggregate

_AMOUNTS = [288163.03, 50325.83, 115356.43]
_CLIENT = {"name": "client", "type": "str", "nullable": False}
_AMOUNT = {"name": "amount", "type": "float", "nullable": False}


def _sum_amounts(group_by: list[str]) -> pd.Series:
    key = [_CLIENT] if group_by else []
    stage = parse_stage({
        "id": "totals", "type": "aggregate", "description": "Totals the amounts.",
        "inputs": [{"id": "filings"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "filings", "columns": [*key, _AMOUNT]}],
            "produces": [*key, {"name": "total", "type": "float", "nullable": True}]},
        "aggregate": {"group_by": group_by, "aggregations": [
            {"output_column": "total", "formula": "sum", "value_column": "amount"}]},
    })
    filings = pd.DataFrame({"client": ["a"] * len(_AMOUNTS), "amount": _AMOUNTS})
    ctx = RunContext.for_stages_outside_a_run(run_dir=None)
    output = handle_aggregate(place_stage(stage), as_inputs({"filings": filings}), ctx)
    return rows_of(output)["total"]


@pytest.mark.parametrize("group_by", [[], ["client"]], ids=["whole frame", "grouped"])
def test_a_float_sum_is_the_exact_decimal_sum(group_by):
    assert {sum(_AMOUNTS), math.fsum(_AMOUNTS)} == {453845.29000000004}

    totals = _sum_amounts(group_by)

    assert totals.dtype == "float64"
    assert totals.iloc[0] == 453845.29
    assert render_figure(float(totals.iloc[0])) == "453,845.29"
