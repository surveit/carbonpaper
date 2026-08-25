"""Every stage type said in English, over the twelve-row grants workflow."""
from __future__ import annotations

import pytest

from app.models.stages.aggregate import AggregateStage
from app.models.workflow import Workflow
from app.web.stage_prose import plan_an_aggregate, say_what_a_stage_did
from scope_fixture import column, stage_specs


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
    assert plan_an_aggregate(stages["grant_totals"]).lead == (
        "Summarize every input row into a single row")


def test_each_output_column_gets_a_sentence_the_source_column_sits_inside(stages):
    plan = plan_an_aggregate(stages["by_portfolio"])
    assert plan.lead == "Summarize into one row per portfolio by grouping input rows"
    assert [(out.column, out.does, out.from_column) for out in plan.outputs] == [
        # `count` counts rows and reads no column, so there is nothing to name.
        ("grants", "how many rows there were", None),
        ("total_amount", "added up from", "amount"),
    ]


def test_a_list_and_a_carried_column_put_their_clause_after_the_source_column(stages):
    # No stage in the fixture reaches for either formula, so the spec is written here.
    plan = plan_an_aggregate(_aggregate_over(stages["one_row_per_grant"], [
        {"output_column": "grant_ids", "formula": "list", "value_column": "grant_id"},
        {"output_column": "region", "formula": "only", "value_column": "region"},
    ]))
    said = {out.column: f"{out.does} {out.from_column}{out.then}" for out in plan.outputs}
    assert said["grant_ids"] == "every grant_id, kept as a list"
    assert said["region"] == (
        "carried from region — the run stops if two rows of the group differ")


def _aggregate_over(source, aggregations):
    return AggregateStage.model_validate({
        "id": "by_portfolio_wide", "type": "aggregate",
        "description": "Every grant of a portfolio, listed.",
        "inputs": [{"id": source.id}],
        "aggregate": {"group_by": ["portfolio"], "aggregations": aggregations},
        "signature": {"form": "replaces", "reads": [], "produces": [
            column("portfolio", "str"), column("grant_ids", "list[str]"),
            column("region", "str")]},
    })
