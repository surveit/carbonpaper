"""Behavior + lineage tests for the sort_rows handler: run it for real through
run_subset (so manifest.json + outputs/*.parquet land on disk exactly like a
production run), then prove app.runtime.trace can walk back through the
permutation to the source row a sorted row came from."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import SubsetRunError
from app.models import parse_stage, Stage, Workflow
from app.models.run_parameters import RunParameters
from app.runtime.executor import run_subset
from app.runtime.stages.sort_rows import handle_sort_rows
from app.runtime.trace import trace_row

_SCHEMA = {"columns": [
    {"name": "a", "type": "str", "nullable": True},
    {"name": "b", "type": "int", "nullable": True},
    {"name": "g", "type": "str", "nullable": True},
]}


def _sort_stage(sid: str, input_id: str, sort: dict) -> Stage:
    return parse_stage({
        "id": sid, "description": sid, "type": "sort_rows",
        "inputs": [{"id": input_id, "schema": _SCHEMA}],
        "signature": {"form": "extends"},
        "sort": sort,
    })


def _load_stage(sid: str, df: pd.DataFrame, tmp_path) -> Stage:
    path = tmp_path / f"{sid}.csv"
    df.to_csv(path, index=False)
    return parse_stage({
        "id": sid, "description": sid, "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _SCHEMA["columns"]},
    })


def _run(stages: list[Stage], stage_ids: list[str], run_dir):
    return run_subset(
        Workflow(stages=stages), injected_outputs={},
        stage_ids=stage_ids, run_dir=run_dir, repo_root=run_dir.parent.parent,
    )


# ── behavior ──────────────────────────────────────────────────────────────────


def test_sort_orders_by_several_keys_each_running_its_own_way(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z", "w"], "b": [2, 1, 2, 1], "g": ["m"] * 4})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {"keys": [
        {"column": "b", "direction": "ascending"},
        {"column": "a", "direction": "descending"},
    ]})

    outputs = _run([load, srt], ["src", "s"], tmp_path / "runs" / "multi")

    # b ascending groups (y, w) before (x, z); a descending orders within each.
    assert outputs["s"]["a"].tolist() == ["y", "w", "z", "x"]
    assert outputs["s"]["b"].tolist() == [1, 1, 2, 2]


def test_sort_keeps_every_row_and_every_column(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [3, 1, 2], "g": ["m", "n", "o"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {"keys": [{"column": "b"}]})

    out = _run([load, srt], ["src", "s"], tmp_path / "runs" / "whole")["s"]

    assert list(out.columns) == ["a", "b", "g"]
    expected = src.sort_values("b").reset_index(drop=True)
    pd.testing.assert_frame_equal(out[["a", "b", "g"]].reset_index(drop=True), expected)


def test_rows_tying_on_every_key_keep_their_input_order(tmp_path):
    # Every row here ties, so a stable sort must return the input.
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [1, 1, 1], "g": ["m", "n", "o"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {"keys": [{"column": "b"}]})

    out = _run([load, srt], ["src", "s"], tmp_path / "runs" / "stable")["s"]

    assert out["a"].tolist() == ["x", "y", "z"]


@pytest.mark.parametrize(
    "nulls,expected", [("last", ["x", "z", "y"]), ("first", ["y", "x", "z"])]
)
def test_nulls_land_where_the_key_says_not_where_the_direction_implies(
    tmp_path, nulls, expected
):
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [1, 2, 3], "g": ["m", None, "n"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {"keys": [{"column": "g", "nulls": nulls}]})

    out = _run([load, srt], ["src", "s"], tmp_path / "runs" / f"nulls_{nulls}")["s"]

    assert out["a"].tolist() == expected


def test_per_key_nulls_differ_within_one_sort(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z", "w"], "b": [1, 1, 2, 2],
                        "g": [None, "n", "o", None]})
    # Opposite nulls placements in one sort — what pandas' single global
    # na_position cannot express, which is why each key carries its own.
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {"keys": [
        {"column": "b", "direction": "ascending", "nulls": "last"},
        {"column": "g", "direction": "ascending", "nulls": "first"},
    ]})

    out = _run([load, srt], ["src", "s"], tmp_path / "runs" / "per_key_nulls")["s"]

    # Within each b group the null `g` sorts first: x before y, w before z.
    assert out["a"].tolist() == ["x", "y", "w", "z"]


# ── the Starlark key form ─────────────────────────────────────────────────────


def test_a_starlark_key_orders_rows_by_something_no_column_holds(tmp_path):
    src = pd.DataFrame({"a": ["xxx", "y", "zz"], "b": [1, 2, 3], "g": ["m", "n", "o"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {
        "code": "def sort_key(row):\n    return [len(row['a'])]\n",
        "summary": "shortest `a` first",
    })

    out = _run([load, srt], ["src", "s"], tmp_path / "runs" / "starlark")["s"]

    assert out["a"].tolist() == ["y", "zz", "xxx"]


def test_a_starlark_key_of_several_values_breaks_its_own_ties(tmp_path):
    src = pd.DataFrame({"a": ["xx", "yy", "z"], "b": [2, 1, 3], "g": ["m", "n", "o"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {
        "code": "def sort_key(row):\n    return [len(row['a']), row['b']]\n",
        "summary": "shortest `a` first, then smallest `b`",
    })

    out = _run([load, srt], ["src", "s"], tmp_path / "runs" / "starlark_multi")["s"]

    assert out["a"].tolist() == ["z", "yy", "xx"]


def test_a_ragged_starlark_key_stops_the_stage(tmp_path):
    src = pd.DataFrame({"a": ["x", "y"], "b": [1, 2], "g": ["m", "n"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {
        "code": (
            "def sort_key(row):\n"
            "    if row['b'] == 1:\n"
            "        return [row['b']]\n"
            "    return [row['b'], row['a']]\n"
        ),
        "summary": "a key whose width depends on the row",
    })

    with pytest.raises(SubsetRunError) as exc_info:
        _run([load, srt], ["src", "s"], tmp_path / "runs" / "ragged")

    assert "ragged" in str(exc_info.value)


def test_a_starlark_key_that_is_not_a_list_stops_the_stage(tmp_path):
    src = pd.DataFrame({"a": ["x", "y"], "b": [1, 2], "g": ["m", "n"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {
        "code": "def sort_key(row):\n    return row['b']\n",
        "summary": "a bare value, not a key",
    })

    with pytest.raises(SubsetRunError) as exc_info:
        _run([load, srt], ["src", "s"], tmp_path / "runs" / "scalar_key")

    assert "must return a list" in str(exc_info.value)


# ── loud failures ─────────────────────────────────────────────────────────────


def test_values_that_cannot_be_compared_stop_the_stage(tmp_path):
    # Not reachable through a declared schema, so call the handler directly.
    srt = _sort_stage("s", "src", {"keys": [{"column": "a"}]})
    mixed = pd.DataFrame({"a": pd.Series(["x", 2, "b", 10], dtype=object),
                          "b": [1, 2, 3, 4], "g": ["m", "n", "o", "p"]})
    # Left to itself pandas ORDERS this key: sorting on the is-null column
    # beside it takes the lexsort path, which factorizes an object column
    # instead of comparing it, and returns [2, 10, 'b', 'x'].

    with pytest.raises(ValueError, match="cannot be compared"):
        handle_sort_rows(srt, {"src": mixed}, None)


def test_a_key_of_one_kind_beside_nulls_still_sorts(tmp_path):
    # The comparability check must not read a null as a second kind of value.
    srt = _sort_stage("s", "src", {"keys": [{"column": "a"}]})
    holey = pd.DataFrame({"a": pd.Series(["x", None, "b"], dtype=object),
                          "b": [1, 2, 3], "g": ["m", "n", "o"]})

    out = handle_sort_rows(srt, {"src": holey}, None)

    assert out["a"].tolist() == ["b", "x", None]


def test_an_empty_input_sorts_to_an_empty_output(tmp_path):
    srt = _sort_stage("s", "src", {"keys": [{"column": "a"}]})
    empty = pd.DataFrame({"a": [], "b": [], "g": []})

    assert len(handle_sort_rows(srt, {"src": empty}, None)) == 0


# ── trace: the point of this file ─────────────────────────────────────────────


def test_trace_walks_through_a_sort_to_the_right_source_row(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z"], "b": [3, 1, 2], "g": ["m", "n", "o"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {"keys": [{"column": "b"}]})
    run_dir = tmp_path / "runs" / "trace_sort"

    _run([load, srt], ["src", "s"], run_dir)

    # s's output row 0 ('y', b=1) is src's row 1: every row survives a sort, so
    # a positional walk would land on 'x' and be confidently wrong.
    trace = trace_row(run_dir, "s", 0)
    assert [step.stage_id for step in trace.steps] == ["s", "src"]
    assert trace.steps[0].row["a"] == "y"
    assert trace.steps[1].row_ordinal == 1
    assert trace.steps[1].row["a"] == "y"
    assert trace.end.reached_origin is True
    assert trace.end.at_stage == "src"

    # And the row the sort moved to the end came from src's row 0.
    last = trace_row(run_dir, "s", 2)
    assert last.steps[1].row_ordinal == 0
    assert last.steps[1].row["a"] == "x"


def test_a_window_sorts_what_the_stage_read_and_lineage_names_true_ordinals(tmp_path):
    src = pd.DataFrame({"a": ["x", "y", "z", "w"], "b": [4, 3, 1, 2],
                        "g": ["m", "n", "o", "p"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {"keys": [{"column": "b"}]})
    run_dir = tmp_path / "runs" / "windowed"
    # offset 1 + limit 2 hands the sort src rows 1-2 only; the permutation it
    # records against that sliced frame counts from the frame's first row, so
    # the executor has to shift it back onto src's own ordinals.
    params = RunParameters(offsets={"s": 1}, limits={"s": 2})

    outputs = run_subset(
        Workflow(stages=[load, srt]), injected_outputs={},
        stage_ids=["src", "s"], run_dir=run_dir, repo_root=tmp_path, params=params,
    )

    # 'w' (b=2) sorts lowest overall but was never offered to the stage.
    assert outputs["s"]["a"].tolist() == ["z", "y"]
    trace = trace_row(run_dir, "s", 0)
    assert trace.steps[1].row_ordinal == 2
    assert trace.steps[1].row["a"] == "z"


def test_trace_crosses_a_sort_driven_by_a_starlark_key(tmp_path):
    src = pd.DataFrame({"a": ["xxx", "y", "zz"], "b": [1, 2, 3], "g": ["m", "n", "o"]})
    load = _load_stage("src", src, tmp_path)
    srt = _sort_stage("s", "src", {
        "code": "def sort_key(row):\n    return [len(row['a'])]\n",
        "summary": "shortest `a` first",
    })
    run_dir = tmp_path / "runs" / "trace_starlark_sort"

    _run([load, srt], ["src", "s"], run_dir)

    trace = trace_row(run_dir, "s", 0)
    assert trace.steps[1].row_ordinal == 1
    assert trace.steps[1].row["a"] == "y"
