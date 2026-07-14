"""Row-level lineage for row-reshaping stages (issue #58).

Reshaping stages — join (fan-out), aggregate (fan-in), human_review_queue
(drop/reorder), and recoverable python_frame_function — record `lineage/<stage>
.parquet` sidecars of `out_row, in_stage, in_row` edges. Row-preserving stages
record nothing (positional identity). These tests cover:

* the slice_edges alignment helper — the stated correctness crux — plus an
  end-to-end --offset/--limit run that proves the persisted sidecar lines up
  with the persisted output;
* per-handler edge recording for join / aggregate / human_review_queue /
  python_frame_function (incl. NaN group keys and where-vs-membership);
* the tracer consuming sidecars back to origin rows.
"""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from app.models import Stage
from app.runtime.lineage import (
    read_lineage,
    slice_edges,
    write_lineage,
)
from app.runtime.runner import execute_run
from app.runtime.stages.aggregate import handle_aggregate
from app.runtime.stages.human_review_queue import handle_human_review_queue
from app.runtime.stages.join import handle_join
from app.runtime.stages.python_functions import handle_python_frame_function
from app.runtime.trace import trace_row, trace_to_origins
from app.services.versioning import create_version, load_version_stages


# ── slice_edges: the alignment crux ──────────────────────────────────────────
def test_slice_edges_offset_drops_and_renumbers():
    edges = [(0, "s", 10), (1, "s", 11), (2, "s", 12)]
    # offset 1 drops out_row 0 and renumbers the rest down by one.
    assert slice_edges(edges, offset=1, limit=None) == [(0, "s", 11), (1, "s", 12)]


def test_slice_edges_limit_caps_after_offset():
    edges = [(0, "s", 10), (1, "s", 11), (2, "s", 12), (3, "s", 13)]
    # offset 1 then cap 2 → keep original rows 1,2 renumbered to 0,1.
    assert slice_edges(edges, offset=1, limit=2) == [(0, "s", 11), (1, "s", 12)]


def test_slice_edges_keeps_all_edges_of_a_surviving_fanned_row():
    # A fanned-out row has several edges sharing one out_row; the slice keeps or
    # drops them together.
    edges = [(0, "l", 0), (0, "r", 0), (1, "l", 1), (1, "r", 9)]
    assert slice_edges(edges, offset=1, limit=None) == [(0, "l", 1), (0, "r", 9)]


def test_slice_edges_offset_past_end_drops_everything():
    assert slice_edges([(0, "s", 0), (1, "s", 1)], offset=5, limit=None) == []


# ── join ─────────────────────────────────────────────────────────────────────
def _join_stage(how: str = "inner") -> Stage:
    return Stage.model_validate({
        "id": "j", "name": "J", "type": "join",
        "inputs": [{"id": "left"}, {"id": "right"}],
        "join": {"type": how, "keys": [{"left": "k", "right": "k"}]},
    })


def test_join_inner_fanout_records_edges_for_both_sides():
    left = pd.DataFrame({"k": ["a", "b"], "lv": [1, 2]})
    right = pd.DataFrame({"k": ["a", "a", "b"], "rv": [10, 11, 20]})
    ctx: dict = {}
    out = handle_join(_join_stage("inner"), {"left": left, "right": right}, ctx)

    # left row a (pos 0) fans out to two right rows (pos 0,1); left row b (pos 1)
    # matches right pos 2.
    assert len(out) == 3
    edges = ctx["lineage"]["j"]
    # Each output row carries one left edge and one right edge.
    by_out: dict[int, dict[str, int]] = {}
    for o, stg, i in edges:
        by_out.setdefault(o, {})[stg] = i
    assert by_out[0] == {"left": 0, "right": 0}
    assert by_out[1] == {"left": 0, "right": 1}
    assert by_out[2] == {"left": 1, "right": 2}


def test_join_outer_unmatched_side_records_no_edge():
    left = pd.DataFrame({"k": ["a", "b"], "lv": [1, 2]})
    right = pd.DataFrame({"k": ["a", "c"], "rv": [10, 30]})
    ctx: dict = {}
    out = handle_join(_join_stage("outer"), {"left": left, "right": right}, ctx)
    assert len(out) == 3  # a (matched), b (left only), c (right only)
    edges = ctx["lineage"]["j"]
    by_out: dict[int, dict[str, int]] = {}
    for o, stg, i in edges:
        by_out.setdefault(o, {})[stg] = i
    # The unmatched b row has only a left edge; the unmatched c row only a right.
    matched = [v for v in by_out.values() if set(v) == {"left", "right"}]
    left_only = [v for v in by_out.values() if set(v) == {"left"}]
    right_only = [v for v in by_out.values() if set(v) == {"right"}]
    assert len(matched) == 1 and len(left_only) == 1 and len(right_only) == 1


# ── aggregate ────────────────────────────────────────────────────────────────
def _agg_stage(aggregations: list[dict], group_by=None) -> Stage:
    return Stage.model_validate({
        "id": "agg", "name": "Agg", "type": "aggregate",
        "inputs": [{"id": "load"}],
        "aggregate": {"group_by": group_by or ["g"], "aggregations": aggregations},
    })


def test_aggregate_group_membership_edges():
    df = pd.DataFrame({"g": ["a", "b", "a", "c", "b"], "v": [1, 2, 3, 4, 5]})
    ctx: dict = {}
    out = handle_aggregate(
        _agg_stage([{"output_column": "total", "formula": "sum", "value_column": "v"}]),
        {"load": df}, ctx,
    )
    # Groups sorted a,b,c → output rows 0,1,2.
    assert list(out["g"]) == ["a", "b", "c"]
    members: dict[int, set[int]] = {}
    for o, stg, i in ctx["lineage"]["agg"]:
        assert stg == "load"
        members.setdefault(o, set()).add(i)
    assert members == {0: {0, 2}, 1: {1, 4}, 2: {3}}


def test_aggregate_nan_group_key_members_matched():
    # dropna=False keeps a NaN group; its members must be matched despite NaN!=NaN.
    df = pd.DataFrame({"g": ["a", None, "a"], "v": [1, 2, 3]})
    ctx: dict = {}
    out = handle_aggregate(
        _agg_stage([{"output_column": "total", "formula": "sum", "value_column": "v"}]),
        {"load": df}, ctx,
    )
    members: dict[int, set[int]] = {}
    for o, stg, i in ctx["lineage"]["agg"]:
        members.setdefault(o, set()).add(i)
    # The NaN-key group traces to its one member (input row 1).
    nan_out = out.index[out["g"].isna()][0]
    assert members[int(nan_out)] == {1}
    a_out = out.index[out["g"] == "a"][0]
    assert members[int(a_out)] == {0, 2}


def test_aggregate_where_narrows_formula_not_membership():
    # `where` changes which rows feed the FORMULA, not which rows BELONG to the
    # group — lineage traces group membership over the full input.
    df = pd.DataFrame({"g": ["a", "a", "b"], "v": [1, 3, 5]})
    ctx: dict = {}
    out = handle_aggregate(
        _agg_stage([{"output_column": "total", "formula": "sum",
                     "value_column": "v", "where": "v > 1"}]),
        {"load": df}, ctx,
    )
    a_total = int(out.loc[out["g"] == "a", "total"].iloc[0])
    assert a_total == 3  # where excluded v=1 from the sum
    members: dict[int, set[int]] = {}
    for o, stg, i in ctx["lineage"]["agg"]:
        members.setdefault(o, set()).add(i)
    a_out = int(out.index[out["g"] == "a"][0])
    assert members[a_out] == {0, 1}  # BOTH a-rows belong, where notwithstanding


# ── human_review_queue ───────────────────────────────────────────────────────
def test_human_review_queue_lineage_tracks_reorder_and_drop(tmp_path):
    df = pd.DataFrame({"entity_id": ["e0", "e1", "e2", "e3"],
                       "score": [0.2, 0.9, 0.5, 0.95]})
    stage = Stage.model_validate({
        "id": "review", "name": "Review", "type": "human_review_queue",
        "inputs": [{"id": "src"}],
        "queue": {"filter": "score >= 0.8", "hash_columns": ["entity_id"]},
    })

    def _h(eid: str) -> str:
        return hashlib.sha1(eid.encode("utf-8")).hexdigest()[:16]

    # e1 approved, e3 rejected (dropped). Passthrough: e0, e2.
    decisions = pd.DataFrame({
        "content_hash": [_h("e1"), _h("e3")],
        "decision": ["approve", "reject"],
        "modified_score": [pd.NA, pd.NA],
        "reviewer": ["r", "r"],
        "reviewed_at": ["t", "t"],
        "source_run_id": ["x", "x"],
    })
    (tmp_path / "decisions").mkdir()
    decisions.to_parquet(tmp_path / "decisions" / "review.parquet", index=False)

    ctx: dict = {"project_dir": tmp_path, "run_dir": tmp_path, "queue_stats": {}}
    out = handle_human_review_queue(stage, {"src": df}, ctx)

    # decided (e1) first, then passthrough (e0, e2); e3 dropped.
    assert list(out["entity_id"]) == ["e1", "e0", "e2"]
    edges = {o: i for o, stg, i in ctx["lineage"]["review"]}
    assert edges == {0: 1, 1: 0, 2: 2}  # out→src position, e3 (src 3) has no edge
    # Hidden lineage column never leaks into the stage output.
    assert not any(c.startswith("__lineage") for c in out.columns)


# ── python_frame_function: recover-or-untracked (design §4.2) ─────────────────
def _frame_stage(code: str) -> Stage:
    return Stage.model_validate({
        "id": "ff", "name": "FF", "type": "python_frame_function",
        "inputs": [{"id": "src"}],
        "function": {"kind": "inline", "code": code},
    })


def test_frame_function_permutation_is_recovered():
    df = pd.DataFrame({"k": ["k0", "k1", "k2"], "v": [1, 2, 3]})
    ctx: dict = {}
    handle_python_frame_function(
        _frame_stage("def transform(df):\n    return df.iloc[::-1].reset_index(drop=True)\n"),
        {"src": df}, ctx,
    )
    edges = {o: i for o, stg, i in ctx["lineage"]["ff"]}
    assert edges == {0: 2, 1: 1, 2: 0}  # reversed


def test_frame_function_reshape_is_untracked():
    df = pd.DataFrame({"g": ["a", "a", "b"], "v": [1, 2, 3]})
    ctx: dict = {}
    handle_python_frame_function(
        _frame_stage("def transform(df):\n    return df.groupby('g', as_index=False)['v'].sum()\n"),
        {"src": df}, ctx,
    )
    assert ctx["lineage"]["ff"] == "untracked"


# ── end-to-end persistence + the --offset/--limit alignment crux ─────────────
def _agg_project(root):
    (root / "compiled").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    pd.DataFrame({"g": ["a", "b", "a", "c", "b"], "v": [1, 2, 3, 4, 5]}) \
        .to_csv(root / "data" / "items.csv", index=False)
    load = {"id": "load", "name": "Load", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": "data/items.csv", "format": "csv"}}}
    agg = {"id": "agg", "name": "Agg", "type": "aggregate",
           "inputs": [{"id": "load"}],
           "aggregate": {"group_by": ["g"],
                         "aggregations": [{"output_column": "total",
                                           "formula": "sum", "value_column": "v"}]}}
    (root / "compiled" / "01_load.json").write_text(json.dumps(load), encoding="utf-8")
    (root / "compiled" / "02_agg.json").write_text(json.dumps(agg), encoding="utf-8")


def test_aggregate_lineage_sidecar_persisted_full_run(tmp_path):
    _agg_project(tmp_path)
    create_version(tmp_path, message="seed", reviewer="t")
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    run_dir = tmp_path / "runs" / manifest["run_id"]

    lin = read_lineage(run_dir, "agg")
    assert list(lin.columns) == ["out_row", "in_stage", "in_row"]
    members = lin.groupby("out_row")["in_row"].apply(lambda s: set(s.tolist())).to_dict()
    assert members == {0: {0, 2}, 1: {1, 4}, 2: {3}}  # a,b,c
    # Row-preserving input_data records no sidecar.
    assert read_lineage(run_dir, "load") is None


def test_aggregate_lineage_aligned_under_offset_and_limit(tmp_path):
    # THE CRUX: edges recorded on pre-slice ordinals must be re-sliced through
    # the same --offset/--limit as the output before they're persisted.
    _agg_project(tmp_path)
    create_version(tmp_path, message="seed", reviewer="t")
    manifest = execute_run(tmp_path, repo_root=tmp_path,
                           offsets={"agg": 1}, limits={"agg": 1})
    run_dir = tmp_path / "runs" / manifest["run_id"]

    out = pd.read_parquet(run_dir / "outputs" / "agg.parquet")
    assert list(out["g"]) == ["b"]  # offset 1 drops a, limit 1 keeps just b

    lin = read_lineage(run_dir, "agg")
    # out_row renumbered to 0 (was 1), still pointing at b's members (input 1,4).
    assert set(lin["out_row"]) == {0}
    assert set(lin["in_row"]) == {1, 4}
    assert set(lin["in_stage"]) == {"load"}


def test_tracer_consumes_aggregate_sidecar_to_origins(tmp_path):
    _agg_project(tmp_path)
    version_id = create_version(tmp_path, message="seed", reviewer="t")["id"]
    manifest = execute_run(tmp_path, repo_root=tmp_path)
    run_dir = tmp_path / "runs" / manifest["run_id"]
    stages = {s.id: s for s in load_version_stages(tmp_path, version_id)}

    # Aggregate output row 0 (group a) traces back through the sidecar to the
    # input_data origin rows 0 and 2 — the tracer no longer stops at the reshape.
    result = trace_to_origins(run_dir, stages, "agg", 0)
    assert result["untracked"] is False
    assert set(result["origins"]) == {("load", 0), ("load", 2)}

    tree = trace_row(run_dir, stages, "agg", 0)
    assert tree["type"] == "aggregate"
    assert {(n["stage"], n["row"]) for n in tree["sources"]} == {("load", 0), ("load", 2)}
    assert all(n["origin"] for n in tree["sources"])


def test_write_and_read_empty_lineage_roundtrips(tmp_path):
    write_lineage(tmp_path, "s", [])
    lin = read_lineage(tmp_path, "s")
    assert lin is not None and lin.empty
    assert list(lin.columns) == ["out_row", "in_stage", "in_row"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
