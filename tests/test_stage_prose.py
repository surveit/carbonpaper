"""Every stage type said in English, over the twelve-row grants workflow."""
from __future__ import annotations

import pytest

from app.models.workflow import Workflow
from app.web.stage_prose import plan_an_aggregate, say_what_a_stage_did
from scope_fixture import stage_specs


@pytest.fixture
def stages(tmp_path):
    workflow = Workflow.model_validate({"stages": stage_specs(tmp_path / "data")})
    return {stage.id: stage for stage in workflow.stages}


def test_a_load_names_the_file_and_not_the_path(stages):
    assert say_what_a_stage_did(stages["load_east"]) == "Load input data from east.csv"


def test_a_union_names_the_tables_it_stacked(stages):
    assert say_what_a_stage_did(stages["both_regions"]) == (
        "Stack load_east and load_west into one table")


def test_a_join_names_both_sides_and_the_keys(stages):
    # Both sides call the key agency_code, so it is said once rather than as a pair.
    assert say_what_a_stage_did(stages["tag_portfolio"]) == (
        "Combine both_regions data with load_agencies data on agency_code")


def test_a_dedupe_names_the_keys_rows_share(stages):
    assert say_what_a_stage_did(stages["one_row_per_grant"]) == (
        "Collapse rows that share the same grant_id")


def test_a_whole_frame_aggregate_says_one_row_comes_out(stages):
    assert say_what_a_stage_did(stages["grant_totals"]) == (
        "Collapse every row into a single row of figures")
    assert plan_an_aggregate(stages["grant_totals"]).lead == "Every row collapses into one row."


def test_an_aggregate_groups_its_outputs_by_what_the_formula_does(stages):
    plan = plan_an_aggregate(stages["by_portfolio"])
    assert plan.lead == "One row per portfolio."
    assert [(group.does, [out.column for out in group.outputs]) for group in plan.groups] == [
        ("Counted — how many rows there were", ["grants"]),
        ("Added up", ["total_amount"]),
    ]


def test_an_output_names_the_column_it_was_worked_out_from(stages):
    plan = plan_an_aggregate(stages["by_portfolio"])
    summed = next(out for group in plan.groups for out in group.outputs
                  if out.column == "total_amount")
    assert summed.from_column == "amount"
    counted = next(out for group in plan.groups for out in group.outputs
                   if out.column == "grants")
    # `count` counts rows and reads no column, so there is nothing to name.
    assert counted.from_column is None
