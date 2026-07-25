"""The oracle for the save-time output derivations: the real handlers, on real
frames.

`app/models/stages/{join,aggregate}.py` predict the columns (and types) a join
or aggregate stage can deliver, so a declared `output_schema` can be rejected at
save time instead of mid-run. Those predictions are claims about
`app/runtime/stages/{join,aggregate}.py` — and the two live in layers that share
no code (the models layer imports no pandas, by design), so nothing but a test
like this stops them drifting apart.

Every case here runs the handler over frames built from the same column spec the
derivation is given, then compares. Joins are `inner` with fully matching keys on
purpose: a left/outer join can widen an int column to float where it fills NaN,
which is nullability — data, not deliverability — and deliberately outside what
the derivation claims.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.models.schema import TableSchema
from app.models.stage import Stage
from app.models.stages.aggregate import (
    derive_aggregate_output_types,
    find_aggregate_output_collisions,
)
from app.models.stages.join import derive_join_output_types, find_join_merge_collisions
from app.runtime.stages.aggregate import handle_aggregate
from app.runtime.stages.join import handle_join
from conftest import make_run_context

# Our type vocabulary against numpy's dtype.kind, which is what the frames the
# handlers return actually carry: pandas' str dtype reports kind "O", as does
# the object dtype a `list` aggregation produces (asserted separately by value).
_DTYPE_KIND_OF = {"str": "O", "int": "i", "float": "f", "bool": "b"}

_LEFT = [
    ("facility_id", "str", ["f1", "f2"]),
    ("name", "str", ["Alpha", "Beta"]),
    ("score", "float", [1.5, 2.5]),
]
_RIGHT = [
    ("facility_id", "str", ["f1", "f2"]),
    ("name", "int", [10, 20]),
    ("amount", "int", [100, 200]),
]


def _schema(columns) -> TableSchema:
    return TableSchema.model_validate(
        {"columns": [{"name": name, "type": type_} for name, type_, _ in columns]}
    )


def _frame(columns) -> pd.DataFrame:
    return pd.DataFrame({name: values for name, _, values in columns})


def _assert_carries(df: pd.DataFrame, derived: dict[str, str | None]) -> None:
    """Every derived column is present with the derived type, and the frame
    carries nothing the derivation missed."""
    assert set(df.columns) == set(derived)
    for name, type_ in derived.items():
        if type_ is None:
            continue
        if type_.startswith("list["):
            assert df[name].dtype.kind == "O"
            assert isinstance(df[name].iloc[0], list)
        else:
            assert df[name].dtype.kind == _DTYPE_KIND_OF[type_], name


def _join_stage(join_config: dict) -> Stage:
    """A join stage whose input edges declare NO schema — the save-time check is
    then inert (it has nothing to derive from), which is what lets the failure
    cases below reach the handler at all."""
    return Stage.model_validate({
        "id": "enrich", "name": "Enrich", "type": "join",
        "inputs": [{"id": "left"}, {"id": "right"}],
        "join": join_config,
    })


def _run_join(stage: Stage, left=_LEFT, right=_RIGHT) -> pd.DataFrame:
    return handle_join(
        stage, {"left": _frame(left), "right": _frame(right)}, make_run_context()
    )


def _aggregate_stage(aggregations, group_by=("company",)) -> Stage:
    return Stage.model_validate({
        "id": "totals", "name": "Totals", "type": "aggregate",
        "inputs": [{"id": "facilities"}],
        "aggregate": {"group_by": list(group_by), "aggregations": aggregations},
    })


_FACILITIES = [
    ("company", "str", ["acme", "acme", "globex"]),
    ("revenue", "int", [10, 20, 30]),
    ("region", "str", ["eu", "eu", "us"]),
    ("margin", "float", [0.1, 0.2, 0.3]),
]


def _run_aggregate(stage: Stage) -> pd.DataFrame:
    return handle_aggregate(stage, {"facilities": _frame(_FACILITIES)}, make_run_context())


# ── join ─────────────────────────────────────────────────────────────────────
def test_derived_join_columns_match_the_merge():
    # Same-name key collapses; `name` collides so the right side arrives as
    # `name_r`; `amount` is uncollided.
    stage = _join_stage({
        "type": "inner", "keys": [{"left": "facility_id", "right": "facility_id"}],
    })
    assert stage.join is not None
    derived = derive_join_output_types(stage.join, _schema(_LEFT), _schema(_RIGHT))
    _assert_carries(_run_join(stage), derived)


def test_derived_join_columns_match_when_key_names_differ():
    left = [("lid", "str", ["f1", "f2"]), ("score", "float", [1.5, 2.5])]
    right = [("rid", "str", ["f1", "f2"]), ("amount", "int", [100, 200])]
    stage = _join_stage({"type": "inner", "keys": [{"left": "lid", "right": "rid"}]})
    assert stage.join is not None
    derived = derive_join_output_types(stage.join, _schema(left), _schema(right))
    # Both key columns survive when their names differ — no collapse.
    assert set(derived) == {"lid", "score", "rid", "amount"}
    _assert_carries(_run_join(stage, left=left, right=right), derived)


def test_select_projects_the_derived_columns():
    stage = _join_stage({
        "type": "inner",
        "keys": [{"left": "facility_id", "right": "facility_id"}],
        "select": ["facility_id", "name_r"],
    })
    assert stage.join is not None
    derived = derive_join_output_types(stage.join, _schema(_LEFT), _schema(_RIGHT))
    projected = {name: derived[name] for name in stage.join.select or []}
    _assert_carries(_run_join(stage), projected)


def test_merge_collision_is_a_real_merge_error():
    # Left already has `x_r`, so suffixing right's `x` collides: pandas refuses
    # the merge outright, and the save-time check names the same column.
    left = [("k", "str", ["a"]), ("x", "int", [1]), ("x_r", "int", [2])]
    right = [("k", "str", ["a"]), ("x", "int", [3])]
    stage = _join_stage({"type": "inner", "keys": [{"left": "k", "right": "k"}]})
    assert stage.join is not None
    with pytest.raises(pd.errors.MergeError):
        _run_join(stage, left=left, right=right)
    issues = find_join_merge_collisions("enrich", stage.join, _schema(left), _schema(right))
    assert len(issues) == 1
    assert "'x_r'" in issues[0]


def test_merge_collision_between_two_right_columns():
    # The mirror image: nothing on the left collides, but right's own `x_r`
    # takes the name right's `x` would be suffixed to.
    left = [("k", "str", ["a"]), ("x", "int", [1])]
    right = [("k", "str", ["a"]), ("x", "int", [3]), ("x_r", "int", [4])]
    stage = _join_stage({"type": "inner", "keys": [{"left": "k", "right": "k"}]})
    assert stage.join is not None
    with pytest.raises(pd.errors.MergeError):
        _run_join(stage, left=left, right=right)
    assert find_join_merge_collisions("enrich", stage.join, _schema(left), _schema(right))


# ── aggregate ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("aggregation", [
    {"output_column": "n", "formula": "count"},
    {"output_column": "total", "formula": "sum", "value_column": "revenue"},
    {"output_column": "total", "formula": "sum", "value_column": "region"},
    {"output_column": "avg", "formula": "mean", "value_column": "revenue"},
    {"output_column": "avg", "formula": "mean", "value_column": "margin"},
    {"output_column": "lo", "formula": "min", "value_column": "revenue"},
    {"output_column": "hi", "formula": "max", "value_column": "margin"},
    {"output_column": "one", "formula": "first", "value_column": "region"},
    {"output_column": "all_of", "formula": "list", "value_column": "revenue"},
])
def test_derived_aggregate_columns_match_the_groupby(aggregation):
    stage = _aggregate_stage([aggregation])
    assert stage.aggregate is not None
    derived = derive_aggregate_output_types(stage.aggregate, _schema(_FACILITIES))
    _assert_carries(_run_aggregate(stage), derived)


def test_derived_aggregate_columns_match_across_several_aggregations():
    stage = _aggregate_stage(
        [
            {"output_column": "n", "formula": "count"},
            {"output_column": "total", "formula": "sum", "value_column": "revenue"},
            {"output_column": "regions", "formula": "list", "value_column": "region"},
        ],
        group_by=("company", "region"),
    )
    assert stage.aggregate is not None
    derived = derive_aggregate_output_types(stage.aggregate, _schema(_FACILITIES))
    _assert_carries(_run_aggregate(stage), derived)


# The three collision cases below cannot be built through `Stage.model_validate`
# at all — unlike join's, the aggregate collision check needs no edge schema, so
# it fires on every such config. Each test therefore edits a VALID stage's
# handle in place (pydantic re-validates neither list mutation nor assignment
# here) purely to reach the handler, which is the point: this is what the save
# -time rejection is protecting against.
def test_duplicate_output_column_loses_both_names_at_runtime():
    # The handler outer-merges its per-aggregation frames, so two aggregations
    # named `total` land as pandas' `total_x`/`total_y`: a stage declaring
    # `total` would fail output validation on a column neither op produced.
    stage = _aggregate_stage([
        {"output_column": "total", "formula": "sum", "value_column": "revenue"},
    ])
    assert stage.aggregate is not None
    stage.aggregate.aggregations.append(
        stage.aggregate.aggregations[0].model_copy(update={"formula": "mean"})
    )
    columns = set(_run_aggregate(stage).columns)
    assert "total" not in columns
    assert {"total_x", "total_y"} <= columns
    issues = find_aggregate_output_collisions("totals", stage.aggregate)
    assert len(issues) == 1
    assert "'total'" in issues[0]


def test_output_column_shadowing_group_by_raises_at_runtime():
    stage = _aggregate_stage([
        {"output_column": "total", "formula": "sum", "value_column": "revenue"},
    ])
    assert stage.aggregate is not None
    stage.aggregate.aggregations[0].output_column = "company"
    with pytest.raises(ValueError):
        _run_aggregate(stage)
    issues = find_aggregate_output_collisions("totals", stage.aggregate)
    assert len(issues) == 1
    assert "'company'" in issues[0]


def test_repeated_group_by_raises_at_runtime():
    stage = _aggregate_stage([{"output_column": "n", "formula": "count"}])
    assert stage.aggregate is not None
    stage.aggregate.group_by.append("company")
    with pytest.raises(ValueError):
        _run_aggregate(stage)
    assert find_aggregate_output_collisions("totals", stage.aggregate)
