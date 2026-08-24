"""explode/dedupe/sort_rank: proves app.runtime.trace walks back to the source row."""
from __future__ import annotations

import json

import pytest

from app.core.errors import SubsetRunError
from app.core.frames import table_to_frame
from app.models import parse_stage, Stage, Workflow
from app.runtime.executor import execute_subset
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
_FLAT_COLUMNS = [column for column in _COLUMNS if column["name"] != "issues"]
_ISSUES_COLUMN = next(c for c in _COLUMNS if c["name"] == "issues")


def _reads(input_id: str, names: list[str], columns=None) -> list[dict]:
    by_name = {c["name"]: c for c in (columns or _COLUMNS)}
    return [{"input": input_id, "columns": [by_name[name] for name in names]}]


def _source_stage(sid: str, rows: list[dict], columns: list[dict], tmp_path) -> Stage:
    path = tmp_path / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return parse_stage({
        "id": sid, "description": sid, "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "json"}},
        "signature": {"form": "replaces", "produces": columns},
    })


def _run(workflow: Workflow, stage_ids: list[str], run_dir):
    return execute_subset(
        workflow, injected_outputs={}, stage_ids=stage_ids,
        run_dir=run_dir, project_id=run_dir.parent.parent.name)


# ── explode ───────────────────────────────────────────────────────────────────
def _explode_stage(sid: str, input_id: str, **config) -> Stage:
    return parse_stage({
        "id": sid, "description": sid, "type": "explode",
        "inputs": [{"id": input_id}],
        # Extends, not replaces: every other column flows through untouched, and the
        # one write is `issues` narrowing from list[str] to the element type.
        "signature": {"form": "extends", "reads": _reads(input_id, ["issues"]),
                      "rewrites": [dict(_ISSUES_COLUMN, type="str")]},
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
        "signature": {"form": "extends",
                      "reads": _reads(input_id, ["client", "amount_usd"])},
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


def test_a_deduped_row_names_only_the_row_that_carried_forward(tmp_path):
    run_dir = tmp_path / "runs" / "r"
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _FLAT_COLUMNS, tmp_path),
        _dedupe_stage("latest", "filings", keep="highest", by="amount_usd"),
    ])
    out = table_to_frame(_run(workflow, ["filings", "latest"], run_dir)["latest"])

    northwind = out.index[out["client"] == "Northwind Resources"][0]
    trace = trace_row(run_dir, "latest", int(northwind))

    # F-1003 (input row 2) survived; F-1001 lost to it and supplies no cell.
    assert _traced_source_ordinals(run_dir, "latest", int(northwind)) == [2]
    assert trace.steps[0].branches == []


def test_dedupe_emits_its_survivors_in_input_order(tmp_path):
    run_dir = tmp_path / "runs" / "r"
    workflow = Workflow(stages=[
        _source_stage("filings", _FILINGS, _FLAT_COLUMNS, tmp_path),
        _dedupe_stage("latest", "filings", keep="highest", by="amount_usd"),
    ])
    _run(workflow, ["filings", "latest"], run_dir)

    kept = [_traced_source_ordinals(run_dir, "latest", i)[0] for i in range(2)]
    assert kept == sorted(kept)


def test_dedupe_keep_first_refuses_a_by_column(tmp_path):
    with pytest.raises(ValueError, match="keep=first picks by position"):
        _dedupe_stage("latest", "filings", keep="first", by="amount_usd")


# ── sort_rank ─────────────────────────────────────────────────────────────────
def _sort_rank_stage(sid: str, input_id: str, keys: list[dict], rank_column=None,
                     columns=None) -> Stage:
    adds = [{"name": rank_column, "type": "int", "nullable": False}] if rank_column else []
    return parse_stage({
        "id": sid, "description": sid, "type": "sort_rank",
        "inputs": [{"id": input_id}],
        "signature": {"form": "extends", "adds": adds,
                      "reads": _reads(input_id, [k["column"] for k in keys], columns)},
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


def _nullable_tier():
    rows = _FILINGS[:2] + [dict(_FILINGS[2], tier=None)]
    return rows, [dict(c, nullable=True) if c["name"] == "tier" else c for c in _FLAT_COLUMNS]


def test_sort_rank_refuses_a_nullable_key_that_does_not_place_its_nulls(tmp_path):
    rows, columns = _nullable_tier()
    with pytest.raises(ValueError, match="set `nulls` on that key"):
        Workflow(stages=[
            _source_stage("filings", rows, columns, tmp_path),
            _sort_rank_stage("ranked", "filings", [{"column": "tier"}], columns=columns),
        ])


@pytest.mark.parametrize(
    "nulls,expected",
    [("last", ["F-1002", "F-1001", "F-1003"]), ("first", ["F-1003", "F-1002", "F-1001"])],
)
def test_sort_rank_places_nulls_where_the_key_says(tmp_path, nulls, expected):
    rows, columns = _nullable_tier()
    workflow = Workflow(stages=[
        _source_stage("filings", rows, columns, tmp_path),
        _sort_rank_stage("ranked", "filings",
                         [{"column": "tier", "nulls": nulls}], columns=columns),
    ])
    out = table_to_frame(_run(workflow, ["filings", "ranked"], tmp_path / "runs" / "r")["ranked"])

    assert list(out["filing_id"]) == expected


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


# ── what the extends signatures refuse ────────────────────────────────────────
# Each of these was unexpressible while the three types declared `replaces`: a
# signature restating the whole schema says nothing about which columns the rule
# consults, so nothing could disagree with it.
def _explode_signature(input_id: str, **signature) -> dict:
    return {
        "id": "issues", "description": "issues", "type": "explode",
        "inputs": [{"id": input_id}], "explode": {"column": "issues"},
        "signature": {"form": "extends", "reads": _reads(input_id, ["issues"]), **signature},
    }


def test_explode_refuses_a_signature_that_does_not_rewrite_the_exploded_column():
    with pytest.raises(ValueError, match="signature rewrites must declare it"):
        parse_stage(_explode_signature("filings", rewrites=[]))


def test_explode_refuses_a_signature_that_adds_a_column():
    with pytest.raises(ValueError, match="explode adds no column"):
        parse_stage(_explode_signature(
            "filings", rewrites=[dict(_ISSUES_COLUMN, type="str")],
            adds=[{"name": "issue_rank", "type": "int", "nullable": False}]))


def test_explode_refuses_a_column_its_input_supplies_as_a_scalar(tmp_path):
    scalar_issues = [dict(c, type="str") for c in _COLUMNS]
    workflow_stages = [
        _source_stage("filings", _FILINGS, scalar_issues, tmp_path),
        parse_stage(_explode_signature("filings", rewrites=[dict(_ISSUES_COLUMN, type="str")])),
    ]
    with pytest.raises(ValueError, match="not a list"):
        Workflow(stages=workflow_stages)


def test_dedupe_refuses_a_signature_that_writes(tmp_path):
    with pytest.raises(ValueError, match="never adds or rewrites"):
        parse_stage({
            "id": "latest", "description": "latest", "type": "dedupe",
            "inputs": [{"id": "filings"}], "dedupe": {"keys": ["client"]},
            "signature": {"form": "extends", "reads": _reads("filings", ["client"]),
                          "adds": [{"name": "kept", "type": "bool", "nullable": False}]},
        })


def test_sort_rank_refuses_an_add_when_no_rank_column_is_named():
    with pytest.raises(ValueError, match="only when `rank_column` names one"):
        parse_stage({
            "id": "ranked", "description": "ranked", "type": "sort_rank",
            "inputs": [{"id": "filings"}], "sort_rank": {"keys": [{"column": "amount_usd"}]},
            "signature": {"form": "extends", "reads": _reads("filings", ["amount_usd"]),
                          "adds": [{"name": "rank", "type": "int", "nullable": False}]},
        })


def test_sort_rank_refuses_a_rank_column_that_is_not_an_int():
    with pytest.raises(ValueError, match="a 1-based position is 'int'"):
        parse_stage({
            "id": "ranked", "description": "ranked", "type": "sort_rank",
            "inputs": [{"id": "filings"}],
            "sort_rank": {"keys": [{"column": "amount_usd"}], "rank_column": "rank"},
            "signature": {"form": "extends", "reads": _reads("filings", ["amount_usd"]),
                          "adds": [{"name": "rank", "type": "str", "nullable": False}]},
        })
