"""Behavior + lineage tests for the explode, dedupe and sort_rank handlers: run
them for real through run_subset, then prove app.runtime.trace walks back through
them to the right source row — the thing a python_frame_function doing the same
work cannot offer."""
from __future__ import annotations

import json

import pytest

from app.core.errors import SubsetRunError
from app.core.frames import table_to_frame
from app.models import parse_stage, Stage, Workflow
from app.runtime.executor import run_subset
from app.runtime.lineage import EdgeKind
from app.runtime.trace import trace_row

# A filing register: one row per lobbying filing, each carrying the issue codes
# the filing listed. The shape an llm_transform leaves behind — many findings
# held as one list column on the row they were read from.
_FILINGS = [
    {"filing_id": "F-1001", "client": "Northwind Resources", "amount_usd": 120000,
     "tier": "T2", "issues": ["TAX", "ENERGY"]},
    {"filing_id": "F-1002", "client": "Cascade Freight", "amount_usd": 45000,
     "tier": "T1", "issues": ["TRANSPORT"]},
    {"filing_id": "F-1003", "client": "Northwind Resources", "amount_usd": 260000,
     "tier": "T1", "issues": []},
]

_COLUMNS = [
    {"name": "filing_id", "type": "str", "nullable": False},
    {"name": "client", "type": "str", "nullable": False},
    {"name": "amount_usd", "type": "int", "nullable": False},
    {"name": "tier", "type": "str", "nullable": False},
    {"name": "issues", "type": "list[str]", "nullable": True},
]
_EXPLODED_COLUMNS = [
    dict(column, type="str") if column["name"] == "issues" else column
    for column in _COLUMNS
]
_FLAT_COLUMNS = [column for column in _COLUMNS if column["name"] != "issues"]


def _source_stage(sid: str, rows: list[dict], columns: list[dict], tmp_path) -> Stage:
    path = tmp_path / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return parse_stage({
        "id": sid, "description": sid, "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "json"}},
        "signature": {"form": "replaces", "produces": columns},
    })


def _run(workflow: Workflow, stage_ids: list[str], run_dir):
    return run_subset(
        workflow, injected_outputs={}, stage_ids=stage_ids,
        run_dir=run_dir, project_id=run_dir.parent.parent.name)


# ── explode ───────────────────────────────────────────────────────────────────
def _explode_stage(sid: str, input_id: str, **config) -> Stage:
    return parse_stage({
        "id": sid, "description": sid, "type": "explode",
        "inputs": [{"id": input_id}],
        "signature": {"form": "replaces", "produces": _EXPLODED_COLUMNS},
        "explode": {"column": "issues", **config},
    })


def test_explode_gives_each_element_its_own_row_and_drops_the_empty_list(tmp_path):
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _COLUMNS, tmp_path),
        _explode_stage("issues", "filings"),
    ])
    out = table_to_frame(_run(workflow, ["filings", "issues"], tmp_path / "runs" / "r")["issues"])

    assert list(out["issues"]) == ["TAX", "ENERGY", "TRANSPORT"]
    # The rest of the row is copied onto each element, F-1003 contributing none.
    assert list(out["filing_id"]) == ["F-1001", "F-1001", "F-1002"]
    assert list(out["amount_usd"]) == [120000, 120000, 45000]


def test_explode_keep_empty_holds_the_row_that_found_nothing(tmp_path):
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _COLUMNS, tmp_path),
        _explode_stage("issues", "filings", keep_empty=True),
    ])
    out = table_to_frame(_run(workflow, ["filings", "issues"], tmp_path / "runs" / "r")["issues"])

    assert list(out["filing_id"]) == ["F-1001", "F-1001", "F-1002", "F-1003"]
    assert out["issues"].isna().tolist() == [False, False, False, True]


def test_trace_walks_an_exploded_row_back_to_the_filing_it_came_from(tmp_path):
    run_dir = tmp_path / "runs" / "r"
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _COLUMNS, tmp_path),
        _explode_stage("issues", "filings"),
    ])
    _run(workflow, ["filings", "issues"], run_dir)

    # Output row 1 is F-1001's second issue code; row 2 is F-1002's only one.
    assert _traced_source_ordinals(run_dir, "issues", 1) == [0]
    assert _traced_source_ordinals(run_dir, "issues", 2) == [1]


# ── dedupe ────────────────────────────────────────────────────────────────────
def _dedupe_stage(sid: str, input_id: str, **config) -> Stage:
    return parse_stage({
        "id": sid, "description": sid, "type": "dedupe",
        "inputs": [{"id": input_id}],
        "signature": {"form": "replaces", "produces": _FLAT_COLUMNS},
        "dedupe": {"keys": ["client"], **config},
    })


def test_dedupe_keeps_the_highest_row_per_key(tmp_path):
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _FLAT_COLUMNS, tmp_path),
        _dedupe_stage("latest", "filings", keep="highest", by="amount_usd"),
    ])
    out = table_to_frame(_run(workflow, ["filings", "latest"], tmp_path / "runs" / "r")["latest"])

    assert sorted(out["filing_id"]) == ["F-1002", "F-1003"]
    assert out.loc[out["client"] == "Northwind Resources", "amount_usd"].tolist() == [260000]


def test_dedupe_records_the_row_it_collapsed_as_well_as_the_survivor(tmp_path):
    run_dir = tmp_path / "runs" / "r"
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _FLAT_COLUMNS, tmp_path),
        _dedupe_stage("latest", "filings", keep="highest", by="amount_usd"),
    ])
    out = table_to_frame(_run(workflow, ["filings", "latest"], run_dir)["latest"])

    northwind = out.index[out["client"] == "Northwind Resources"][0]
    trace = trace_row(run_dir, "latest", int(northwind))

    # The spine reaches F-1003 (input row 2), the row that survived...
    assert _traced_source_ordinals(run_dir, "latest", int(northwind)) == [2]
    # ...and F-1001 (input row 0), which lost to it, is still named as collapsed into it.
    assert [b.row_ordinal for b in trace.steps[0].branches] == [0]
    assert all(b.kind == EdgeKind.contribution.value for b in trace.steps[0].branches)


def test_dedupe_keep_first_refuses_a_by_column(tmp_path):
    with pytest.raises(ValueError, match="keep=first picks by position"):
        _dedupe_stage("latest", "filings", keep="first", by="amount_usd")


# ── sort_rank ─────────────────────────────────────────────────────────────────
def _sort_rank_stage(sid: str, input_id: str, keys: list[dict], rank_column=None,
                     columns=None) -> Stage:
    produces = list(columns or _FLAT_COLUMNS)
    if rank_column:
        produces = produces + [{"name": rank_column, "type": "int", "nullable": False}]
    return parse_stage({
        "id": sid, "description": sid, "type": "sort_rank",
        "inputs": [{"id": input_id}],
        "signature": {"form": "replaces", "produces": produces},
        "sort_rank": {"keys": keys, **({"rank_column": rank_column} if rank_column else {})},
    })


def test_sort_rank_orders_by_the_stated_value_order_then_numbers_the_rows(tmp_path):
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _FLAT_COLUMNS, tmp_path),
        _sort_rank_stage("ranked", "filings",
                         [{"column": "tier", "order": ["T1", "T2"]},
                          {"column": "amount_usd", "descending": True}],
                         rank_column="rank"),
    ])
    out = table_to_frame(_run(workflow, ["filings", "ranked"], tmp_path / "runs" / "r")["ranked"])

    # T1 before T2 because the config says so, not because "T1" sorts before "T2";
    # within T1 the larger amount leads.
    assert list(out["filing_id"]) == ["F-1003", "F-1002", "F-1001"]
    assert list(out["rank"]) == [1, 2, 3]


def test_sort_rank_refuses_a_value_its_stated_order_does_not_rank(tmp_path):
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _FLAT_COLUMNS, tmp_path),
        _sort_rank_stage("ranked", "filings", [{"column": "tier", "order": ["T1"]}]),
    ])
    with pytest.raises(SubsetRunError, match=r"\['T2'\]"):
        _run(workflow, ["filings", "ranked"], tmp_path / "runs" / "r")


def test_sort_rank_refuses_a_null_in_a_sort_key(tmp_path):
    rows = _FILINGS[:2] + [dict(_FILINGS[2], tier=None)]
    columns = [dict(c, nullable=True) if c["name"] == "tier" else c for c in _FLAT_COLUMNS]
    workflow = Workflow(stages=[
        _source_stage("filings", rows, columns, tmp_path),
        _sort_rank_stage("ranked", "filings", [{"column": "tier"}], columns=columns),
    ])
    with pytest.raises(SubsetRunError, match="1 of 3 rows hold no value"):
        _run(workflow, ["filings", "ranked"], tmp_path / "runs" / "r")


def test_trace_follows_a_row_to_where_the_sort_moved_it_from(tmp_path):
    run_dir = tmp_path / "runs" / "r"
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _FLAT_COLUMNS, tmp_path),
        _sort_rank_stage("ranked", "filings", [{"column": "amount_usd", "descending": True}]),
    ])
    _run(workflow, ["filings", "ranked"], run_dir)

    # Largest amount first: output row 0 is input row 2, and output row 2 is input row 1.
    assert _traced_source_ordinals(run_dir, "ranked", 0) == [2]
    assert _traced_source_ordinals(run_dir, "ranked", 2) == [1]


def _traced_source_ordinals(run_dir, stage_id: str, row: int) -> list[int]:
    steps = trace_row(run_dir, stage_id, row).steps
    return sorted(step.row_ordinal for step in steps if step.stage_id == "filings")
