"""Tests for app/web/column_walk.py — the backward walk and the writer graph."""
from __future__ import annotations

import pytest

from app import models as m
from app.web.column_walk import (
    ColumnAt,
    WalkStop,
    build_writer_graph,
    find_nearest_writer_upstream,
    order_sheet_columns,
    walk_column_back,
)

FILING_COLUMNS = ["client", "income", "expenses"]


def _column(name, type_="str"):
    return {"name": name, "type": type_, "nullable": True}


def _filings(stage_id):
    return m.parse_stage({
        "id": stage_id, "description": stage_id, "type": "input_data",
        "connector": {"kind": "file", "params": {"paths": [f"/filings/{stage_id}.csv"]}},
        "signature": {"form": "replaces",
                      "produces": [_column(name) for name in FILING_COLUMNS]},
    })


def _union(stage_id, inputs):
    return m.parse_stage({
        "id": stage_id, "description": stage_id, "type": "union",
        "inputs": [{"id": upstream} for upstream in inputs], "union": {},
        "signature": {"form": "extends"},
    })


def _row_function(stage_id, source, reads, adds=(), rewrites=()):
    return m.parse_stage({
        "id": stage_id, "description": stage_id, "type": "python_row_function",
        "inputs": [{"id": source}],
        "function": {"kind": "inline", "code": "def transform(row): return row"},
        "signature": {
            "form": "extends",
            "reads": [{"input": source, "columns": [_column(name) for name in reads]}],
            "adds": [_column(name) for name in adds],
            "rewrites": [_column(name) for name in rewrites],
        },
    })


def _spend_by_client(source):
    return m.parse_stage({
        "id": "spend_by_client", "description": "spend_by_client", "type": "aggregate",
        "inputs": [{"id": source}],
        "aggregate": {
            "group_by": ["client_matched"],
            "aggregations": [
                {"formula": "sum", "output_column": "total_income", "value_column": "income"},
                {"formula": "count", "output_column": "row_count"},
            ],
        },
        "signature": {
            "form": "replaces",
            "reads": [{"input": source,
                       "columns": [_column("client_matched"), _column("income")]}],
            "produces": [_column("client_matched"), _column("total_income"),
                         _column("row_count", "int")],
        },
    })


@pytest.fixture
def stages():
    """Two filing sources stacked, matched to one name, then summed per client."""
    return m.Workflow(stages=[
        _filings("input_q1_filings"),
        _filings("input_q2_filings"),
        _union("all_filings", ["input_q1_filings", "input_q2_filings"]),
        _row_function("match_client_aliases", "all_filings",
                      reads=["client"], adds=["client_matched"]),
        _row_function("stand_unmerged_names_alone", "match_client_aliases",
                      reads=["client", "client_matched"], rewrites=["client_matched"]),
        _spend_by_client("stand_unmerged_names_alone"),
    ]).index_workflow_stages_by_id()


def test_an_aggregation_walks_back_to_its_value_column(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "total_income"))
    assert walk.nodes[ColumnAt("spend_by_client", "total_income")].parents == (
        ColumnAt("stand_unmerged_names_alone", "income"),
    )


def test_a_group_key_walks_back_to_the_same_column(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "client_matched"))
    assert walk.nodes[ColumnAt("spend_by_client", "client_matched")].parents == (
        ColumnAt("stand_unmerged_names_alone", "client_matched"),
    )


def test_a_count_reads_no_column_so_the_walk_stops(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "row_count"))
    node = walk.nodes[ColumnAt("spend_by_client", "row_count")]
    assert node.parents == () and node.stop is WalkStop.counts_rows


def test_an_input_stage_is_a_root(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "total_income"))
    assert walk.nodes[ColumnAt("input_q1_filings", "income")].stop is WalkStop.root


def test_a_carried_column_at_a_union_comes_from_every_side(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "total_income"))
    assert walk.nodes[ColumnAt("all_filings", "income")].parents == (
        ColumnAt("input_q1_filings", "income"), ColumnAt("input_q2_filings", "income"),
    )


def test_a_stage_that_wrote_nothing_is_contracted_out_of_the_graph(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "total_income"))
    parents = build_writer_graph(walk, stages).list_parents("spend_by_client")
    assert [edge.from_stage for edge in parents] == [
        "input_q1_filings", "input_q2_filings",
    ]


def test_a_rewrite_sends_the_reader_to_whoever_wrote_it_before(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "client_matched"))
    graph = build_writer_graph(walk, stages)
    assert find_nearest_writer_upstream(
        walk, graph, "stand_unmerged_names_alone", "client_matched"
    ) == "match_client_aliases"


def test_a_carried_column_sends_the_reader_to_the_stage_that_wrote_it(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "client_matched"))
    graph = build_writer_graph(walk, stages)
    assert find_nearest_writer_upstream(
        walk, graph, "match_client_aliases", "client"
    ) == "input_q1_filings"


def test_the_oldest_column_holds_the_first_slot(stages):
    walk = walk_column_back(stages, ColumnAt("spend_by_client", "client_matched"))
    order = order_sheet_columns(walk)
    assert order.index("client") < order.index("client_matched")
