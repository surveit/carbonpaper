"""Behavior + lineage for starlark_filter_rows, and the predicates it has to be
able to express: the shapes the python filter_rows predicates in real workflows
actually use — membership, case-folding, a numeric threshold, refusing a row it
cannot decide."""
from __future__ import annotations

import json

import pytest

from app.core.errors import SubsetRunError
from app.core.frames import table_to_frame
from app.models import parse_stage, Stage, Workflow
from app.runtime.executor import execute_subset
from app.runtime.trace import trace_row

_FILINGS = [
    {"filing_id": "F-1001", "client": "Northwind Resources", "status": "Active",
     "amount_usd": 120000},
    {"filing_id": "F-1002", "client": "Cascade Freight", "status": "terminated",
     "amount_usd": 45000},
    {"filing_id": "F-1003", "client": "Blue Ridge Mining", "status": "ACTIVE",
     "amount_usd": 260000},
]
_COLUMNS = [
    {"name": "filing_id", "type": "str", "nullable": False},
    {"name": "client", "type": "str", "nullable": False},
    {"name": "status", "type": "str", "nullable": False},
    {"name": "amount_usd", "type": "int", "nullable": False},
]


def _source_stage(sid: str, rows: list[dict], tmp_path) -> Stage:
    path = tmp_path / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return parse_stage({
        "id": sid, "description": sid, "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "json"}},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    })


def _filter_stage(sid: str, input_id: str, code: str, reads: list[str]) -> Stage:
    by_name = {c["name"]: c for c in _COLUMNS}
    return parse_stage({
        "id": sid, "description": sid, "type": "starlark_filter_rows",
        "inputs": [{"id": input_id}],
        "signature": {"form": "extends",
                      "reads": [{"input": input_id,
                                 "columns": [by_name[name] for name in reads]}]},
        "starlark_filter": {"code": code},
    })


def _run(workflow: Workflow, stage_ids: list[str], run_dir):
    return execute_subset(workflow, injected_outputs={}, stage_ids=stage_ids,
                      run_dir=run_dir, project_id=run_dir.parent.parent.name)


def _kept(code: str, reads: list[str], tmp_path, rows=None) -> list[str]:
    workflow = Workflow(stages=[
        _source_stage("filings", rows or _FILINGS, tmp_path),
        _filter_stage("in_scope", "filings", code, reads),
    ])
    out = _run(workflow, ["filings", "in_scope"], tmp_path / "runs" / "r")["in_scope"]
    return list(table_to_frame(out)["filing_id"])


# ── the predicate shapes real filter_rows stages use ──────────────────────────
def test_membership_against_a_tuple(tmp_path):
    code = 'def should_include(row):\n    return row["status"] in ("Active", "ACTIVE")\n'
    assert _kept(code, ["status"], tmp_path) == ["F-1001", "F-1003"]


def test_case_folding_and_strip(tmp_path):
    code = 'def should_include(row):\n    return row["status"].strip().lower() == "active"\n'
    assert _kept(code, ["status"], tmp_path) == ["F-1001", "F-1003"]


def test_a_numeric_threshold(tmp_path):
    code = 'def should_include(row):\n    return row["amount_usd"] >= 100000\n'
    assert _kept(code, ["amount_usd"], tmp_path) == ["F-1001", "F-1003"]


def test_a_named_predicate(tmp_path):
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, tmp_path),
        parse_stage({
            "id": "big", "description": "big", "type": "starlark_filter_rows",
            "inputs": [{"id": "filings"}],
            "signature": {"form": "extends", "reads": [
                {"input": "filings", "columns": [
                    c for c in _COLUMNS if c["name"] == "amount_usd"]}]},
            "starlark_filter": {
                "function": "is_large",
                "code": 'def is_large(row):\n    return row["amount_usd"] > 200000\n'},
        }),
    ])
    out = _run(workflow, ["filings", "big"], tmp_path / "runs" / "r")["big"]
    assert list(table_to_frame(out)["filing_id"]) == ["F-1003"]


# ── what it refuses ───────────────────────────────────────────────────────────
def test_a_row_it_cannot_decide_is_refused_not_guessed(tmp_path):
    rows = _FILINGS[:2] + [dict(_FILINGS[2], status="")]
    code = (
        'def should_include(row):\n'
        '    if not row["status"]:\n'
        '        refuse("blank status: cannot tell whether this filing is in scope")\n'
        '    return row["status"].lower() == "active"\n'
    )
    with pytest.raises(SubsetRunError, match="blank status"):
        _kept(code, ["status"], tmp_path, rows=rows)


def test_a_non_bool_return_stops_the_step(tmp_path):
    code = 'def should_include(row):\n    return row["status"]\n'
    with pytest.raises(SubsetRunError, match="must return bool"):
        _kept(code, ["status"], tmp_path)


def test_the_sandbox_refuses_an_import_when_the_stage_is_saved():
    with pytest.raises(ValueError, match="does not compile"):
        _filter_stage("in_scope", "filings",
                      'import re\ndef should_include(row):\n    return True\n', ["status"])


def test_a_signature_that_reads_nothing_is_refused():
    with pytest.raises(ValueError, match="signature reads nothing"):
        parse_stage({
            "id": "in_scope", "description": "in_scope", "type": "starlark_filter_rows",
            "inputs": [{"id": "filings"}],
            "signature": {"form": "extends", "reads": []},
            "starlark_filter": {"code": 'def should_include(row):\n    return True\n'},
        })


def test_a_signature_that_writes_is_refused():
    with pytest.raises(ValueError, match="never adds or rewrites"):
        parse_stage({
            "id": "in_scope", "description": "in_scope", "type": "starlark_filter_rows",
            "inputs": [{"id": "filings"}],
            "signature": {"form": "extends",
                          "reads": [{"input": "filings", "columns": [_COLUMNS[2]]}],
                          "adds": [{"name": "kept", "type": "bool", "nullable": False}]},
            "starlark_filter": {"code": 'def should_include(row):\n    return True\n'},
        })


# ── lineage ───────────────────────────────────────────────────────────────────
def test_trace_walks_a_kept_row_back_to_the_filing_it_came_from(tmp_path):
    run_dir = tmp_path / "runs" / "r"
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, tmp_path),
        _filter_stage("in_scope", "filings",
                      'def should_include(row):\n'
                      '    return row["status"].lower() == "active"\n', ["status"]),
    ])
    _run(workflow, ["filings", "in_scope"], run_dir)

    # Output row 1 is F-1003, which was input row 2 — F-1002 was dropped between them.
    steps = trace_row(run_dir, "in_scope", 1).steps
    assert [s.row_ordinal for s in steps if s.stage_id == "filings"] == [2]
