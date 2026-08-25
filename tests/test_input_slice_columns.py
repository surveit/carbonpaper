"""The column closure over tests/scope_fixture.py, whose graph every case names."""
from __future__ import annotations

from pathlib import Path

from app.models.workflow import parse_workflow
from app.services.input_slice import find_columns_behind
from scope_fixture import stage_specs

# What a `by_portfolio` row came through: the deduped grants and everything above them.
PORTFOLIO_ROUTE = {"by_portfolio", "one_row_per_grant", "funded", "size_band",
                   "tag_portfolio", "both_regions", "load_east", "load_west",
                   "load_agencies"}


def _workflow():
    return parse_workflow(stage_specs(Path("/tmp/scope-fixture")))


def test_a_summed_column_reaches_both_source_files():
    behind = find_columns_behind(_workflow(), PORTFOLIO_ROUTE,
                                 "by_portfolio", "total_amount")
    assert behind["load_east"] == {"grant_id", "agency_code", "amount"}
    assert behind["load_west"] == behind["load_east"]


def test_the_reference_side_of_a_join_carries_its_key_and_what_it_landed():
    behind = find_columns_behind(_workflow(), PORTFOLIO_ROUTE,
                                 "by_portfolio", "total_amount")
    assert behind["load_agencies"] == {"agency_code", "portfolio"}


def test_a_column_no_stage_on_the_route_read_is_left_out():
    behind = find_columns_behind(_workflow(), PORTFOLIO_ROUTE,
                                 "by_portfolio", "total_amount")
    # `kind` is read by grants_only, which a by_portfolio row never passed through,
    # and `region` is read by nothing at all.
    assert "kind" not in behind["load_east"]
    assert "region" not in behind["load_east"]


def test_a_filter_off_the_route_adds_its_predicate_column():
    behind = find_columns_behind(_workflow(),
                                 PORTFOLIO_ROUTE | {"grants_only"},
                                 "by_portfolio", "total_amount")
    assert "kind" in behind["load_east"]


def test_a_group_key_is_needed_as_a_value_and_as_a_membership():
    behind = find_columns_behind(_workflow(), PORTFOLIO_ROUTE,
                                 "by_portfolio", "total_amount")
    assert "portfolio" in behind["one_row_per_grant"]
    assert behind["by_portfolio"] == {"total_amount"}
