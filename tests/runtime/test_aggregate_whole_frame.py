"""`group_by: []` on real data: the whole frame reduces to exactly one row."""
from __future__ import annotations

import pandas as pd
import pytest

from app.models import parse_stage
from app.runtime.context import RunContext
from app.runtime.lineage import LINEAGE_ATTR, EdgeKind, read_row_lineage
from app.runtime.stages.aggregate import handle_aggregate
from app.runtime.trace import trace_row
from test_trace_helpers import write_run

# A repeated firm, a missing one, and amounts either side of the `where` below.
FILINGS = pd.DataFrame({
    "firm": ["a", None, "a", "b", "b"],
    "amt": [10, 20, 30, 40, 50],
})
_IN_SCHEMA = {"columns": [{"name": "firm", "type": "str", "nullable": True},
                          {"name": "amt", "type": "int", "nullable": False}]}
_EMPTY = pd.DataFrame({"firm": pd.Series([], dtype="object"),
                       "amt": pd.Series([], dtype="int64")})

# One aggregation per formula the runtime dispatches on, so the whole-frame path
# is exercised across all eight rather than on the two the project happens to use.
_ALL_FORMULAS = [
    {"output_column": "total", "formula": "sum", "value_column": "amt"},
    {"output_column": "avg", "formula": "mean", "value_column": "amt"},
    {"output_column": "n_rows", "formula": "count"},
    {"output_column": "n_firms", "formula": "count_distinct", "value_column": "firm"},
    {"output_column": "lowest", "formula": "min", "value_column": "amt"},
    {"output_column": "highest", "formula": "max", "value_column": "amt"},
    {"output_column": "first_firm", "formula": "first", "value_column": "firm"},
    {"output_column": "all_amts", "formula": "list", "value_column": "amt"},
]
_ALL_PRODUCES = [
    {"name": "total", "type": "int", "nullable": True},
    {"name": "avg", "type": "float", "nullable": True},
    {"name": "n_rows", "type": "int", "nullable": True},
    {"name": "n_firms", "type": "int", "nullable": True},
    {"name": "lowest", "type": "int", "nullable": True},
    {"name": "highest", "type": "int", "nullable": True},
    {"name": "first_firm", "type": "str", "nullable": True},
    {"name": "all_amts", "type": "list[int]", "nullable": True},
]
_BIG_N = {"output_column": "big_n", "formula": "count", "where": "amt > 25"}
_BIG_N_COL = {"name": "big_n", "type": "int", "nullable": True}


def _reads(aggregations):
    """Exactly the columns the config consumes — a pinned set would fail its own cross-check."""
    consumed = {op["value_column"] for op in aggregations if op.get("value_column")}
    consumed.update(op["where"].split()[0] for op in aggregations if op.get("where"))
    return [c for c in _IN_SCHEMA["columns"] if c["name"] in consumed]


def _stage(aggregations, produces):
    return parse_stage({
        "id": "agg", "type": "aggregate", "name": "agg",
        "inputs": [{"id": "filings", "schema": _IN_SCHEMA}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "filings", "columns": _reads(aggregations)}],
            "produces": produces},
        "aggregate": {"group_by": [], "aggregations": aggregations},
    })


def _run(aggregations, produces, frame=FILINGS) -> pd.DataFrame:
    ctx = RunContext.for_stages_outside_a_run(repo_root=None, run_dir=None)
    return handle_aggregate(_stage(aggregations, produces), {"filings": frame}, ctx)


def _all_formulas(frame=FILINGS) -> dict:
    return _run(_ALL_FORMULAS, _ALL_PRODUCES, frame).iloc[0].to_dict()


def test_the_whole_frame_reduces_to_one_row():
    out = _run(_ALL_FORMULAS, _ALL_PRODUCES)
    assert len(out) == 1


def test_the_output_columns_are_the_aggregations_alone():
    out = _run(_ALL_FORMULAS, _ALL_PRODUCES)
    assert list(out.columns) == [op["output_column"] for op in _ALL_FORMULAS]


@pytest.mark.parametrize("column,expected", [
    ("total", 150),
    ("avg", 30.0),
    ("n_rows", 5),        # count takes no value_column: it counts ROWS
    ("n_firms", 2),       # a, b — the null firm is not a distinct value
    ("lowest", 10),
    ("highest", 50),
    ("first_firm", "a"),
    ("all_amts", [10, 20, 30, 40, 50]),
])
def test_each_formula_reduces_the_whole_frame(column, expected):
    assert _all_formulas()[column] == expected


def test_a_where_narrows_only_its_own_aggregation():
    row = _run([*_ALL_FORMULAS, _BIG_N], [*_ALL_PRODUCES, _BIG_N_COL]).iloc[0]
    # 30, 40 and 50 clear `amt > 25`; the unfiltered count still sees all five.
    assert row["big_n"] == 3
    assert row["n_rows"] == 5


def test_no_trace_column_reaches_the_output():
    out = _run(_ALL_FORMULAS, _ALL_PRODUCES)
    assert not [c for c in out.columns if c.startswith("_trace")]


def test_a_count_is_a_whole_number():
    assert _run(_ALL_FORMULAS, _ALL_PRODUCES)["n_rows"].dtype.kind == "i"


# ---- The empty input frame -------------------------------------------------
# One row, not zero: with no group keys the group is declared by the config
# rather than found in the data, so it exists whatever the input holds. Every
# figure in it is NULL — 0 is an outcome, claiming something was measured and
# found to be none, and nothing was measured here.

def test_an_empty_input_still_emits_exactly_one_row():
    assert len(_run(_ALL_FORMULAS, _ALL_PRODUCES, _EMPTY)) == 1


@pytest.mark.parametrize("column", [op["output_column"] for op in _ALL_FORMULAS])
def test_every_formula_over_an_empty_input_reports_null(column):
    # Counting formulas included: `count` of nothing is absent, not 0.
    assert pd.isna(_all_formulas(_EMPTY)[column])


def test_a_where_that_admits_no_row_reports_null_too():
    none_pass = {"output_column": "big_n", "formula": "count", "where": "amt > 999"}
    # Emptiness is emptiness — a predicate that admitted nothing reads the same
    # as an input that held nothing, and neither reads as a measured zero.
    assert pd.isna(_run([none_pass], [_BIG_N_COL]).iloc[0]["big_n"])


def test_an_empty_slice_matches_what_the_grouped_path_emits_for_that_group():
    none_pass = {"output_column": "big_n", "formula": "count", "where": "amt > 999"}
    # The discriminating check: one constant group key makes the grouped path
    # emit exactly one row too, so the two paths are directly comparable. The
    # grouped path drops a group no row survived from that aggregation's partial
    # and the outer merge fills it null; the whole-frame path must agree.
    total = {"output_column": "total", "formula": "sum", "value_column": "amt"}
    produces = [{"name": "total", "type": "int", "nullable": True}, _BIG_N_COL]
    whole = _run([total, none_pass], produces).iloc[0]

    amt = [c for c in _IN_SCHEMA["columns"] if c["name"] == "amt"]
    key = {"name": "k", "type": "str", "nullable": False}
    grouped = parse_stage({
        "id": "agg", "type": "aggregate", "name": "agg",
        "inputs": [{"id": "filings", "schema": {"columns": [*_IN_SCHEMA["columns"], key]}}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "filings", "columns": [*amt, key]}],
            "produces": [key, *produces]},
        "aggregate": {"group_by": ["k"], "aggregations": [total, none_pass]},
    })
    ctx = RunContext.for_stages_outside_a_run(repo_root=None, run_dir=None)
    by_group = handle_aggregate(grouped, {"filings": FILINGS.assign(k="all")}, ctx).iloc[0]

    assert whole["total"] == by_group["total"]
    assert pd.isna(whole["big_n"]) and pd.isna(by_group["big_n"])


# ---- Lineage ---------------------------------------------------------------

def test_every_input_row_is_a_contributor_to_the_single_row():
    out = _run(_ALL_FORMULAS, _ALL_PRODUCES)
    lineage = read_row_lineage(out)
    assert len(lineage) == 1
    assert [p.row_ordinal for p in lineage.parents[0]] == list(range(len(FILINGS)))
    assert all(p.kind == EdgeKind.contribution.value for p in lineage.parents[0])


def test_a_where_narrows_which_column_a_contributor_is_recorded_against():
    lineage = read_row_lineage(_run([*_ALL_FORMULAS, _BIG_N], [*_ALL_PRODUCES, _BIG_N_COL]))
    by_ordinal = {p.row_ordinal: p.columns for p in lineage.parents[0]}
    # amt=10 fell outside `amt > 25`, so it is not one of the rows behind big_n.
    assert "big_n" not in by_ordinal[0]
    assert "big_n" in by_ordinal[4]


def test_an_empty_input_records_one_row_with_no_contributors():
    lineage = read_row_lineage(_run(_ALL_FORMULAS, _ALL_PRODUCES, _EMPTY))
    # [[]] not [] — the row exists and the run recorded that nothing fed it.
    assert lineage.parents == [[]]


def test_a_contributor_is_a_promotable_starting_point(tmp_path):
    out = _run(_ALL_FORMULAS, _ALL_PRODUCES)
    lineage = read_row_lineage(out)
    out.attrs.pop(LINEAGE_ATTR, None)  # the executor pops it before persisting
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": FILINGS},
        {"id": "agg", "type": "aggregate", "parents": ["filings"],
         "df": out, "lineage": lineage},
    ])

    trace = trace_row(run_dir, "agg", 0)

    assert [b.row_ordinal for b in trace.steps[0].branches] == list(range(len(FILINGS)))
    assert trace.end.reached_origin is False
    assert "summarizes" in trace.end.message
    promoted = trace_row(run_dir, "filings", trace.steps[0].branches[4].row_ordinal)
    assert promoted.steps[0].row["amt"] == 50
    assert promoted.end.reached_origin is True


def test_the_walk_says_no_row_fed_an_empty_group_rather_than_blaming_position(tmp_path):
    out = _run(_ALL_FORMULAS, _ALL_PRODUCES, _EMPTY)
    lineage = read_row_lineage(out)
    out.attrs.pop(LINEAGE_ATTR, None)
    run_dir = write_run(tmp_path, [
        {"id": "filings", "type": "input_data", "parents": [], "df": _EMPTY},
        {"id": "agg", "type": "aggregate", "parents": ["filings"],
         "df": out, "lineage": lineage},
    ])

    end = trace_row(run_dir, "agg", 0).end

    # The run DID record this row's provenance, so the stop must not read as the
    # unrecorded-lineage case ("it reshapes rows … issue #58") — that would blame
    # a gap the run does not have.
    assert "no input row fed it" in end.message
    assert "#58" not in end.message


# ---- The one row has to survive the executor's output checks ----------------

def _output_report(frame: pd.DataFrame):
    from app.models.schema import TableSchema
    from app.runtime.validation import validate_dataframe

    out = _run(_ALL_FORMULAS, _ALL_PRODUCES, frame)
    schema = TableSchema.model_validate({"columns": _ALL_PRODUCES})
    return out, validate_dataframe(out, schema, stage_id="agg", phase="output")


@pytest.mark.parametrize("frame", [FILINGS, _EMPTY], ids=["rows", "empty"])
def test_the_one_row_passes_its_declared_output_schema(frame):
    # An error-severity output issue fails the stage outright.
    _, report = _output_report(frame)
    assert report.ok, [i.message for i in report.issues]


@pytest.mark.parametrize("frame", [FILINGS, _EMPTY], ids=["rows", "empty"])
def test_the_one_row_round_trips_through_parquet(frame, tmp_path):
    out, _ = _output_report(frame)
    out.attrs.pop(LINEAGE_ATTR, None)
    out.to_parquet(tmp_path / "out.parquet", index=False)
    assert len(pd.read_parquet(tmp_path / "out.parquet")) == 1
