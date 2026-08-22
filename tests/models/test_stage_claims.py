from __future__ import annotations

from app.models import parse_stage

_READS = [{"input": "spend_by_client", "columns": [
    {"name": "total_income_usd", "type": "float", "nullable": False}]}]
_SHAPE = "9f2c4e7a1b3d4e5f8a0c2d4e6f8a0b1c"


def _stage(claims, group_by=None):
    return parse_stage({
        "id": "count_client_figures",
        "type": "aggregate",
        "description": "Client-side figures",
        "inputs": [{"id": "spend_by_client"}],
        "signature": {"form": "replaces", "reads": _READS, "produces": [
            {"name": "external_spend", "type": "float", "nullable": True}]},
        "aggregate": {
            "group_by": group_by or [],
            "aggregations": [{"output_column": "external_spend", "formula": "sum",
                              "value_column": "total_income_usd"}],
        },
        **({"claims": claims} if claims is not None else {}),
    })


def test_a_stage_names_the_column_that_states_a_shape():
    stated = _stage([{"shape_id": _SHAPE, "column": "external_spend"}]).claims
    assert [(c.shape_id, c.column) for c in stated] == [(_SHAPE, "external_spend")]


def test_a_stage_states_nothing_by_default():
    assert _stage(None).claims is None


def test_one_stage_can_state_several_shapes():
    other = "1a2b3c4d5e6f708192a3b4c5d6e7f809"
    stage = _stage([{"shape_id": _SHAPE, "column": "external_spend"},
                    {"shape_id": other, "column": "external_spend"}])
    assert [c.shape_id for c in stage.claims] == [_SHAPE, other]


def test_any_stage_type_may_state_a_claim():
    # A count is the obvious source, but nothing here is aggregate-only.
    stage = parse_stage({
        "id": "select_core_filings",
        "type": "filter_rows",
        "description": "Filings in scope",
        "inputs": [{"id": "flag_in_house_filings"}],
        "signature": {"form": "extends", "reads": [{"input": "flag_in_house_filings",
            "columns": [{"name": "in_scope", "type": "bool", "nullable": False}]}]},
        "filter": {"code": "def should_include(row):\n    return row['in_scope']\n"},
        "claims": [{"shape_id": _SHAPE, "column": "total_income_usd"}],
    })
    assert [c.column for c in stage.claims] == ["total_income_usd"]
