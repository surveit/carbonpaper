"""A `sum` over a float column, grouped or over the whole frame."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from conftest import as_inputs, place_stage, rows_of

from app.core.figure_text import render_figure
from app.models import parse_stage
from app.models.errors import StepRefused
from app.runtime.context import RunContext
from app.runtime.stages.aggregate import handle_aggregate

_CENTS = [288163.03, 50325.83, 115356.43]
_THIRDS = [1 / 3] * 3
_CLIENT = {"name": "client", "type": "str", "nullable": False}
_AMOUNT = {"name": "amount", "type": "float", "nullable": False}
_BOTH_PATHS = pytest.mark.parametrize(
    "group_by", [[], ["client"]], ids=["whole frame", "grouped"])


def _sum_amounts(amounts: list[float], group_by: list[str]) -> pd.Series:
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
    filings = pd.DataFrame({"client": ["a"] * len(amounts), "amount": amounts})
    ctx = RunContext.for_stages_outside_a_run(run_dir=None)
    output = handle_aggregate(place_stage(stage), as_inputs({"filings": filings}), ctx)
    return rows_of(output)["total"]


@_BOTH_PATHS
def test_cents_sum_to_the_exact_decimal_where_fsum_lands_one_ulp_off(group_by):
    assert {sum(_CENTS), math.fsum(_CENTS)} == {453845.29000000004}

    totals = _sum_amounts(_CENTS, group_by)

    assert totals.dtype == "float64"
    assert totals.iloc[0] == 453845.29
    assert render_figure(float(totals.iloc[0])) == "453,845.29"


@_BOTH_PATHS
def test_computed_floats_sum_as_fsum_does(group_by):
    assert _sum_amounts(_THIRDS, group_by).iloc[0] == math.fsum(_THIRDS) == 1.0


@_BOTH_PATHS
def test_both_infinities_refuse_the_step_and_say_where(group_by):
    with pytest.raises(StepRefused) as refused:
        _sum_amounts([math.inf, -math.inf, 1.0], group_by)

    message = str(refused.value)
    assert "stage 'totals'" in message
    assert ("the group client='a'" if group_by else "the whole frame") in message
    assert "`amount` holds both +inf and -inf" in message
