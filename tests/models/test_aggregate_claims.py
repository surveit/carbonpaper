from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage
from app.models.stages.aggregate import read_declared_claims

_READS = [{"input": "spend_by_client", "columns": [
    {"name": "total_income_usd", "type": "float", "nullable": False}]}]


def _stage(group_by, claims, produces=None):
    return parse_stage({
        "id": "count_client_figures",
        "type": "aggregate",
        "description": "Client-side figures",
        "inputs": [{"id": "spend_by_client"}],
        "signature": {"form": "replaces", "reads": _READS,
                      "produces": produces or [
                          {"name": "external_spend", "type": "float", "nullable": True}]},
        "aggregate": {
            "group_by": group_by,
            "aggregations": [{"output_column": "external_spend", "formula": "sum",
                              "value_column": "total_income_usd"}],
            "claims": claims,
        },
    })


def test_a_reduction_may_declare_the_claim_its_column_states():
    stage = _stage([], [{"label": "Paid to outside firms", "column": "external_spend"}])
    assert [c.column for c in read_declared_claims(stage)] == ["external_spend"]


def test_a_stage_declaring_no_claim_reads_as_none():
    assert read_declared_claims(_stage([], [])) == []


def test_a_grouped_aggregate_may_not_declare_a_claim():
    # It computes a row per group, so no single cell answers the claim.
    produces = [
        {"name": "client_org", "type": "str", "nullable": False},
        {"name": "external_spend", "type": "float", "nullable": True},
    ]
    with pytest.raises(ValidationError, match="a row per group rather than one row"):
        _stage(["client_org"],
               [{"label": "Paid to outside firms", "column": "external_spend"}], produces)


def test_a_claim_naming_a_column_the_aggregate_does_not_compute_is_refused():
    with pytest.raises(ValidationError, match="total_expenses_usd"):
        _stage([], [{"label": "Paid to outside firms", "column": "total_expenses_usd"}])
