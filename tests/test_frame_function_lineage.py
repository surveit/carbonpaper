"""A python_frame_function must account for every row it returns; the trace crosses it on that word."""
from __future__ import annotations

import json

import pyarrow as pa
import pytest

from app.core.errors import RowOutOfRange, StageNotInRun, SubsetRunError
from app.models import parse_stage, Stage, StageType, Workflow
from app.models.run_manifest import RunKind
from app.models.run_parameters import RunParameters
from app.runtime.errors import MissingLineage
from app.runtime.executor import execute_subset
from app.runtime.lineage import EdgeKind, LineageRecorder
from app.runtime.manifest import read_run_manifest
from app.runtime.stage_output import StageOutput
from app.runtime.stages import HANDLERS
from app.runtime.stages.execution import FrameTransformHandler
from app.runtime.trace import trace_row

# One row per lobbying filing; two are the same client, which the frame function collapses.
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
_CLIENT_COLUMNS = [{"name": "client", "type": "str", "nullable": False}]

# Each client's first filing is the row its total was built from; the rest contributed to it.
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

_SILENT_CODE = '''def transform(filings):
    return (filings.groupby("client", sort=True)["amount_usd"].sum()
            .reset_index().rename(columns={"amount_usd": "total_usd"}))
'''

_WATCH_LIST_CODE = '''import pandas as pd


def transform(filings, *, lineage):
    clients = sorted(set(filings["client"]))
    for out, client in enumerate(clients):
        lineage.built_from(out, "filings", int(filings.index[filings["client"] == client][0]))
    # On the watch list with no filing: this row is the code's own.
    lineage.originates(len(clients))
    return pd.DataFrame({"client": [*clients, "Harbor Mills"]})
'''


def _source_stage(tmp_path) -> Stage:
    path = tmp_path / "filings.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in _FILINGS), encoding="utf-8")
    return parse_stage({
        "id": "filings", "description": "Filings", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "json"}},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    })


def _frame_stage(code: str, produces: list[dict] = _TOTALS_COLUMNS) -> Stage:
    return parse_stage({
        "id": "by_client", "description": "Collapse filings by client",
        "type": "python_frame_function", "inputs": [{"id": "filings"}],
        "function": {"kind": "inline", "code": code},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "filings", "columns": _COLUMNS}],
            "produces": produces,
        },
    })


def _run(tmp_path, stage: Stage, params: RunParameters = RunParameters()):
    workflow = Workflow(stages=[_source_stage(tmp_path), stage])
    run_dir = tmp_path / "project" / "runs" / "r1"
    execute_subset(workflow, injected_outputs={}, stage_ids=["filings", stage.id],
                   run_dir=run_dir, params=params, kind=RunKind.production,
                   project_id="project")
    return run_dir


def _read_stage_error_type(stage_id: str) -> str | None:
    manifest = read_run_manifest("project", "r1", RunKind.production)
    record = next(r for r in manifest.stage_records if r.stage_id == stage_id)
    return None if record.error is None else record.error.type


def test_a_recorded_account_lets_the_trace_cross_the_stage(tmp_path):
    run_dir = _run(tmp_path, _frame_stage(_RECORDING_CODE))

    # Row 0 is Cascade Freight (one filing); row 1 is Northwind (two).
    trace = trace_row(run_dir, "by_client", 1)
    assert [step.stage_id for step in trace.steps] == ["by_client", "filings"]
    assert trace.end.reached_origin
    # The filing the row was built from is the spine; the other is a branch to promote.
    branches = trace.steps[0].branches
    assert [(b.stage_id, b.row_ordinal, b.kind) for b in branches] == [
        ("filings", 2, EdgeKind.contribution.value)]
    assert branches[0].columns == ("total_usd",)


def test_a_function_without_the_recorder_fails_its_run_naming_the_fix(tmp_path):
    with pytest.raises(SubsetRunError) as refused:
        _run(tmp_path, _frame_stage(_SILENT_CODE))

    assert _read_stage_error_type("by_client") == MissingLineage.__name__
    assert "`def transform(filings, *, lineage)`" in str(refused.value)
    assert "`lineage.originates(row)`" in str(refused.value)


def test_a_partial_account_is_refused(tmp_path):
    code = _RECORDING_CODE.replace(
        'lineage.built_from(out, "filings", ordinals[0])',
        'if client != "Cascade Freight":\n'
        '            lineage.built_from(out, "filings", ordinals[0])')

    with pytest.raises(SubsetRunError, match="not spoken for, first at 0"):
        _run(tmp_path, _frame_stage(code))
    assert _read_stage_error_type("by_client") == MissingLineage.__name__


def test_a_row_declared_to_originate_ends_the_trace_there(tmp_path):
    run_dir = _run(tmp_path, _frame_stage(_WATCH_LIST_CODE, produces=_CLIENT_COLUMNS))

    trace = trace_row(run_dir, "by_client", 2)
    assert [step.stage_id for step in trace.steps] == ["by_client"]
    assert "originates here" in trace.end.message


def test_rows_recorded_under_an_offset_trace_to_their_true_upstream_rows(tmp_path):
    run_dir = _run(tmp_path, _frame_stage(_RECORDING_CODE),
                   RunParameters(offsets={"by_client": 1}))

    # The function saw F-1002 and F-1003 as its rows 0 and 1; Northwind is output row 1.
    trace = trace_row(run_dir, "by_client", 1)
    assert trace.steps[1].row_ordinal == 2
    assert trace.steps[1].row["filing_id"] == "F-1003"


def test_a_built_stage_whose_handler_reports_no_lineage_writes_no_rows(tmp_path, monkeypatch):
    silent = FrameTransformHandler(lambda stage, inputs, ctx: StageOutput(
        pa.table({"client": ["Cascade Freight"], "total_usd": [45000]})))
    monkeypatch.setitem(HANDLERS, StageType.aggregate, silent)
    totals = parse_stage({
        "id": "totals", "description": "Total by client", "type": "aggregate",
        "inputs": [{"id": "filings"}],
        "aggregate": {"group_by": ["client"], "aggregations": [
            {"output_column": "total_usd", "formula": "sum", "value_column": "amount_usd"}]},
        "signature": {"form": "replaces",
                      "reads": [{"input": "filings", "columns": _COLUMNS[1:]}],
                      "produces": _TOTALS_COLUMNS},
    })

    with pytest.raises(SubsetRunError, match="builds its own rows"):
        _run(tmp_path, totals)
    assert _read_stage_error_type("totals") == MissingLineage.__name__
    assert not (tmp_path / "project" / "runs" / "r1" / "outputs" / "totals.parquet").exists()


def _recorder(rows: int = 3) -> LineageRecorder:
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
        recorder.require_every_row(1)


def test_the_refusal_counts_the_rows_left_unaccounted():
    recorder = _recorder()
    recorder.built_from(0, "filings", 0)
    with pytest.raises(MissingLineage, match="1 of 3 output row"):
        recorder.require_every_row(3)
