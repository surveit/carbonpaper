"""A python_frame_function that declares `lineage` reports its own provenance.

Every other stage type has a shape the runtime can read the provenance off. This one
reshapes arbitrarily, so the authored code is the only thing that knows — which makes
its account its word, and makes checking that word the point of these tests.
"""
from __future__ import annotations

import json

import pytest

from app.core.errors import LineageIncomplete, RowOutOfRange, StageNotInRun, SubsetRunError
from app.models import parse_stage, Stage, Workflow
from app.runtime.executor import execute_subset
from app.runtime.lineage import EdgeKind, LineageRecorder
from app.runtime.trace import trace_row

# One row per lobbying filing; two of them are the same client, which is what the
# frame function below collapses.
_FILINGS = [
    {"filing_id": "F-1001", "client": "Northwind Resources", "amount_usd": 120000},
    {"filing_id": "F-1002", "client": "Cascade Freight", "amount_usd": 45000},
    {"filing_id": "F-1003", "client": "Northwind Resources", "amount_usd": 260000},
]
_COLUMNS = [
    {"name": "filing_id", "type": "str", "nullable": False},
    {"name": "client", "type": "str", "nullable": False},
    {"name": "amount_usd", "type": "int", "nullable": False},
]
_TOTALS_COLUMNS = [
    {"name": "client", "type": "str", "nullable": False},
    {"name": "total_usd", "type": "int", "nullable": False},
]

# Groups filings by client, naming the group's first filing as the row it was built
# from and every other filing as a contributor to `total_usd`.
_RECORDING_CODE = '''import pandas as pd


def transform(filings, *, lineage):
    rows = []
    for client, group in filings.groupby("client", sort=True):
        ordinals = [int(i) for i in group.index]
        out = len(rows)
        lineage.built_from(out, "filings", ordinals[0])
        for other in ordinals[1:]:
            lineage.contributed_by(out, "filings", other, columns=["total_usd"])
        rows.append({"client": client, "total_usd": int(group["amount_usd"].sum())})
    return pd.DataFrame(rows, columns=["client", "total_usd"])
'''

_SILENT_CODE = '''import pandas as pd


def transform(filings):
    return (filings.groupby("client", sort=True)["amount_usd"].sum()
            .reset_index().rename(columns={"amount_usd": "total_usd"}))
'''


def _source_stage(tmp_path) -> Stage:
    path = tmp_path / "filings.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in _FILINGS), encoding="utf-8")
    return parse_stage({
        "id": "filings", "description": "Filings", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "json"}},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    })


def _totals_stage(code: str) -> Stage:
    return parse_stage({
        "id": "totals", "description": "Total by client",
        "type": "python_frame_function", "inputs": [{"id": "filings"}],
        "function": {"kind": "inline", "code": code, "summary": "Totals each client's filings."},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "filings", "columns": _COLUMNS}],
            "produces": _TOTALS_COLUMNS,
        },
    })


def _run(tmp_path, code: str):
    workflow = Workflow(stages=[_source_stage(tmp_path), _totals_stage(code)])
    run_dir = tmp_path / "project" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    execute_subset(workflow, injected_outputs={}, stage_ids=["filings", "totals"],
                   run_dir=run_dir, project_id="project")
    return run_dir


def test_a_recorded_account_lets_the_trace_cross_the_stage(tmp_path):
    run_dir = _run(tmp_path, _RECORDING_CODE)

    # Row 0 is Cascade Freight (one filing); row 1 is Northwind (two).
    trace = trace_row(run_dir, "totals", 1)
    assert [step.stage_id for step in trace.steps] == ["totals", "filings"]
    assert trace.end.reached_origin
    # The filing the row was built from is the spine; the other is a branch the
    # reader can promote, which is what makes a total interrogable.
    branches = trace.steps[0].branches
    assert [(b.stage_id, b.row_ordinal, b.kind) for b in branches] == [
        ("filings", 2, EdgeKind.contribution.value)]
    assert branches[0].columns == ("total_usd",)


def test_a_function_that_declares_nothing_reports_nothing(tmp_path):
    """The trace stops here, as it did before the recorder existed."""
    run_dir = _run(tmp_path, _SILENT_CODE)

    trace = trace_row(run_dir, "totals", 1)
    assert [step.stage_id for step in trace.steps] == ["totals"]
    assert not trace.end.reached_origin


def test_a_partial_account_is_refused(tmp_path):
    code = _RECORDING_CODE.replace(
        'lineage.built_from(out, "filings", ordinals[0])',
        'if client != "Cascade Freight":\n            lineage.built_from(out, "filings", ordinals[0])')
    with pytest.raises(SubsetRunError, match="not spoken for"):
        _run(tmp_path, code)


def test_a_minted_row_says_so_rather_than_staying_silent(tmp_path):
    """`originates` is how a row with no parent passes the refusal above."""
    code = _RECORDING_CODE.replace(
        'lineage.built_from(out, "filings", ordinals[0])',
        'lineage.originates(out)')
    run_dir = _run(tmp_path, code)

    trace = trace_row(run_dir, "totals", 0)
    assert [step.stage_id for step in trace.steps] == ["totals"]
    assert not trace.end.reached_origin


def _recorder(rows: int = 3) -> LineageRecorder:
    import pyarrow as pa
    return LineageRecorder({"filings": pa.table({"filing_id": ["a"] * rows})})


def test_a_row_the_input_does_not_have_is_refused():
    with pytest.raises(RowOutOfRange, match="out of range for input 'filings'"):
        _recorder().built_from(0, "filings", 7)


def test_a_stage_this_one_does_not_read_is_refused():
    with pytest.raises(StageNotInRun, match="was not given 'elsewhere'"):
        _recorder().built_from(0, "elsewhere", 0)


def test_an_account_of_rows_the_stage_did_not_return_is_refused():
    recorder = _recorder()
    recorder.built_from(0, "filings", 0)
    recorder.built_from(5, "filings", 1)
    with pytest.raises(RowOutOfRange, match=r"recorded for output row\(s\) \[5\]"):
        recorder.resolve(1)


def test_resolve_names_how_many_rows_went_unspoken_for():
    recorder = _recorder()
    recorder.built_from(0, "filings", 0)
    with pytest.raises(LineageIncomplete, match="1 of 3 output row"):
        recorder.resolve(3)
