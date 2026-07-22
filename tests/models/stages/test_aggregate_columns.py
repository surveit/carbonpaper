"""Tests for the aggregate stage's config-column check (app/models/stages/
aggregate.py, wired into Stage._config_columns_resolve): `group_by`, each
aggregation's `value_column`, and every column an aggregation's `where`
references must resolve against the stage's own input edge schema — checked
at Stage construction, not just at workflow load."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage


def _aggregate_stage(*, group_by, edge_columns, value_column=None, where=None, formula="count",
                     output_n_type="int"):
    aggregation = {"output_column": "n", "formula": formula}
    if value_column is not None:
        aggregation["value_column"] = value_column
    if where is not None:
        aggregation["where"] = where
    # output_schema's first column is named after group_by[0] (never a
    # generic placeholder): the aggregate handle emits each group_by column
    # under its own name, so a declared column must match it to be
    # deliverable — see test_aggregate_output_schema.py.
    return {
        "id": "agg", "type": "aggregate", "name": "agg",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": c, "type": "str", "nullable": False} for c in edge_columns],
        }}],
        # The aggregated column's declared type must match the formula's
        # derivation: count derives int; sum over these all-str edge columns
        # derives str (concatenation) — see derive_aggregate_output_types.
        "output_schema": {"columns": [{"name": group_by[0], "type": "str", "nullable": False},
                                      {"name": "n", "type": output_n_type, "nullable": False}]},
        "aggregate": {"group_by": group_by, "aggregations": [aggregation]},
    }


def test_group_by_missing_column_rejected():
    with pytest.raises(ValidationError):
        Stage.model_validate(_aggregate_stage(group_by=["nope"], edge_columns=["a"]))


def test_group_by_present_ok():
    Stage.model_validate(_aggregate_stage(group_by=["a"], edge_columns=["a"]))


def test_value_column_missing_rejected():
    with pytest.raises(ValidationError):
        Stage.model_validate(_aggregate_stage(
            group_by=["a"], edge_columns=["a"], value_column="missing", formula="sum",
        ))


def test_value_column_present_ok():
    Stage.model_validate(_aggregate_stage(
        group_by=["a"], edge_columns=["a", "n"], value_column="n", formula="sum",
        output_n_type="str",  # sum over a str edge column derives str
    ))


def test_where_missing_column_rejected():
    with pytest.raises(ValidationError):
        Stage.model_validate(_aggregate_stage(group_by=["a"], edge_columns=["a"], where="ghost_col > 0"))


def test_where_valid_column_ok():
    Stage.model_validate(_aggregate_stage(group_by=["a"], edge_columns=["a"], where="a IS NOT NULL"))


def test_where_unparseable_predicate_rejected():
    """A `where` outside the predicate grammar is a config-column issue too
    (app.models.stages.shared.find_predicate_column_issues turns the
    PredicateError into an issue string), not a raw uncaught exception."""
    with pytest.raises(ValidationError):
        Stage.model_validate(_aggregate_stage(group_by=["a"], edge_columns=["a"], where="`weird name` == 1"))


def test_no_edge_schema_declared_is_skipped_not_flagged():
    """Edge-only resolution: when the input edge declares no schema at all,
    a bad-looking group_by is unresolvable, not wrong — skipped rather than
    flagged."""
    stage = {
        "id": "agg", "type": "aggregate", "name": "agg", "inputs": ["src"],
        "output_schema": {"columns": [{"name": "nope", "type": "str", "nullable": False},
                                      {"name": "n", "type": "int", "nullable": False}]},
        "aggregate": {"group_by": ["nope"], "aggregations": [{"output_column": "n", "formula": "count"}]},
    }
    Stage.model_validate(stage)


def test_column_declared_only_on_a_sibling_producer_is_not_enough():
    """EDGE-only, never a producer-output union: even if some OTHER stage in
    the workflow produces `sector`, this stage's own edge only declares `a` —
    so `sector` is rejected. (There is no sibling stage here at all; a single
    Stage's own construction is the whole check, by design.)"""
    with pytest.raises(ValidationError):
        Stage.model_validate(_aggregate_stage(group_by=["sector"], edge_columns=["a"]))
