"""An aggregate records which input rows fed each output row, and which of its
columns each one fed — read off the same grouping the numbers come from."""
from __future__ import annotations

import pandas as pd

from app.models import parse_stage
from app.runtime.lineage import (
    LINEAGE_ATTR,
    EdgeKind,
    RowLineage,
    read_row_lineage,
)
from app.runtime.stages.aggregate import handle_aggregate
from app.runtime.trace import trace_row
from test_trace_helpers import write_run

# Two firms and a MISSING one, so the dropna=False group is exercised throughout.
FILINGS = pd.DataFrame({
    "firm": ["a", None, "a", None, "b"],
    "amt": [10, 20, 30, 40, 50],
})
_IN_SCHEMA = {"columns": [{"name": "firm", "type": "str", "nullable": True},
                          {"name": "amt", "type": "int", "nullable": False}]}


def _stage(aggregations, out_columns):
    return parse_stage({
        "id": "agg", "type": "aggregate", "description": "agg",
        "inputs": [{"id": "filings", "schema": _IN_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "filings", "columns": _IN_SCHEMA["columns"]}],
            "produces": [{"name": "firm", "type": "str", "nullable": True}, *out_columns]},
        "aggregate": {"group_by": ["firm"], "aggregations": aggregations},
    })


_TOTAL = {"output_column": "total", "formula": "sum", "value_column": "amt"}
_BIG_N = {"output_column": "big_n", "formula": "count", "where": "amt > 25"}
_TOTAL_COL = {"name": "total", "type": "int", "nullable": True}
_BIG_N_COL = {"name": "big_n", "type": "int", "nullable": True}


def _persistable(out: pd.DataFrame):
    lineage = read_row_lineage(out)
    out.attrs.pop(LINEAGE_ATTR, None)
    # The executor pops the channel before persisting because `.attrs` is not
    # JSON-serializable and parquet refuses the frame outright.
    return out, lineage


def _parents_of(lineage: RowLineage, out_row: int):
    return [(p.row_ordinal, p.columns) for p in lineage.parents[out_row]]


def test_every_contributing_row_is_recorded_once_whatever_it_fed():
    out = handle_aggregate(_stage([_TOTAL, _BIG_N], [_TOTAL_COL, _BIG_N_COL]),
                           {"filings": FILINGS}, None)
    lineage = read_row_lineage(out)
    assert lineage is not None
    assert len(lineage) == len(out)
    # Five input rows, five parent entries across the whole sidecar: a row that
    # fed BOTH columns appears once carrying both, not once per column. This is
    # what keeps the sidecar O(input rows) rather than O(rows x aggregations).
    assert sum(len(entry) for entry in lineage.parents) == len(FILINGS)


def test_a_where_narrows_which_column_a_row_is_recorded_against():
    out = handle_aggregate(_stage([_TOTAL, _BIG_N], [_TOTAL_COL, _BIG_N_COL]),
                           {"filings": FILINGS}, None)
    lineage = read_row_lineage(out)
    firm_a = list(out["firm"]).index("a")
    # amt=10 fell outside `amt > 25`, so it fed the total and nothing else,
    # while amt=30 fed both. Recording the group cohort would say both rows fed
    # big_n, overstating what is behind that number.
    assert _parents_of(lineage, firm_a) == [(0, ("total",)), (2, ("total", "big_n"))]


def test_the_missing_group_key_keeps_its_own_contributors():
    out = handle_aggregate(_stage([_TOTAL], [_TOTAL_COL]), {"filings": FILINGS}, None)
    lineage = read_row_lineage(out)
    missing = [i for i, firm in enumerate(out["firm"]) if pd.isna(firm)]
    assert len(missing) == 1
    # dropna=False gives NaN its own group; the rows behind it are the rows that
    # had no firm, not a silently dropped set.
    assert _parents_of(lineage, missing[0]) == [(1, ("total",)), (3, ("total",))]


def test_an_unfiltered_row_is_recorded_against_every_column():
    second = {"output_column": "biggest", "formula": "max", "value_column": "amt"}
    lineage = read_row_lineage(handle_aggregate(
        _stage([_TOTAL, second], [_TOTAL_COL, {"name": "biggest", "type": "int",
                                               "nullable": True}]),
        {"filings": FILINGS}, None))
    # With no `where` anywhere, every contributor fed every number.
    assert all(p.columns == ("total", "biggest")
               for entry in lineage.parents for p in entry)


def test_no_trace_column_reaches_the_output():
    out = handle_aggregate(_stage([_TOTAL], [_TOTAL_COL]), {"filings": FILINGS}, None)
    assert not [c for c in out.columns if c.startswith("_trace")]
    assert list(out.columns) == ["firm", "total"]


def test_the_sidecar_round_trips_through_parquet(tmp_path):
    lineage = read_row_lineage(handle_aggregate(
        _stage([_TOTAL, _BIG_N], [_TOTAL_COL, _BIG_N_COL]), {"filings": FILINGS}, None))
    path = tmp_path / "sidecar.parquet"
    lineage.to_frame().to_parquet(path, index=False)
    assert RowLineage.from_frame(pd.read_parquet(path)) == lineage


def test_the_walk_reports_contributors_and_stops(tmp_path):
    out, lineage = _persistable(
        handle_aggregate(_stage([_TOTAL], [_TOTAL_COL]), {"filings": FILINGS}, None))
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "agg", "type": "aggregate", "parents": ["filings"],
         "df": out, "lineage": lineage},
    ])
    firm_a = list(out["firm"]).index("a")

    trace = trace_row(run_dir, "agg", firm_a)

    assert [s.stage_id for s in trace.steps] == ["agg"]
    assert trace.end.reached_origin is False
    assert "summarizes" in trace.end.message
    assert [b.row_ordinal for b in trace.steps[0].branches] == [0, 2]
    assert all(b.kind == EdgeKind.contribution.value for b in trace.steps[0].branches)


def test_a_contributor_carries_the_columns_it_fed_into_the_payload(tmp_path):
    from app.runtime.trace import trace_to_dict
    out, lineage = _persistable(handle_aggregate(
        _stage([_TOTAL, _BIG_N], [_TOTAL_COL, _BIG_N_COL]), {"filings": FILINGS}, None))
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "agg", "type": "aggregate", "parents": ["filings"],
         "df": out, "lineage": lineage},
    ])
    firm_a = list(out["firm"]).index("a")

    payload = trace_to_dict(trace_row(run_dir, "agg", firm_a))

    # The rendered payload says which number each contributor is behind, so a
    # reader asking about `big_n` is not handed the rows behind `total`.
    assert payload["steps"][0]["branches"] == [
        {"stage_id": "filings", "row_ordinal": 0, "kind": "contribution",
         "columns": ["total"]},
        {"stage_id": "filings", "row_ordinal": 2, "kind": "contribution",
         "columns": ["total", "big_n"]},
    ]


def test_a_contributor_is_a_promotable_starting_point(tmp_path):
    out, lineage = _persistable(
        handle_aggregate(_stage([_TOTAL], [_TOTAL_COL]), {"filings": FILINGS}, None))
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "agg", "type": "aggregate", "parents": ["filings"],
         "df": out, "lineage": lineage},
    ])
    branch = trace_row(run_dir, "agg", list(out["firm"]).index("b")).steps[0].branches[0]

    promoted = trace_row(run_dir, branch.stage_id, branch.row_ordinal)

    assert promoted.steps[0].row["amt"] == 50
    assert promoted.end.reached_origin is True
