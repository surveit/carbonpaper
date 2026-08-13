"""The rows a step's examples are selected from: which run supplies them, what is
narrowed away, and what a search over them reports."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.core.errors import NoRowsToSelectFrom, PredicateError
from app.models import Stage, parse_stage
from app.models.stages.signature import transform_input_schemas
from app.services import workspace
from app.core.row_search import MAX_MATCHES
from app.services.stage_test_rows import load_stage_row_sources

_AMOUNT = {"name": "amount", "type": "str", "nullable": True}
_MEMO = {"name": "memo", "type": "str", "nullable": True}

# What one LDA-shaped upstream wrote: an amount as text, and a memo the step never reads.
_AMOUNTS = ["30000.00", None, "111650.94", "0", "45000.00"]
_MEMOS = ["Q1 filing", "no report", "Q2 filing", "nil return", "Q1 filing"]


def _stage(reads: list[dict[str, Any]] | None = None) -> Stage:
    return parse_stage({
        "id": "read_money", "description": "Read the reported money",
        "type": "python_row_function", "inputs": [{"id": "load"}],
        "function": {"kind": "inline", "summary": "Reads `amount` as a number.",
                     "code": "def transform(row):\n    return {'usd': 0.0}\n"},
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": reads or [_AMOUNT]}],
            "adds": [{"name": "usd", "type": "float", "nullable": False}],
        },
    })


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    examples = tmp_path / "examples"
    project = examples / "demo"
    project.mkdir(parents=True)
    workspace.set_projects_dir(examples)
    return project


def _write_run(
    project_dir: Path,
    run_id: str,
    *,
    frame: pd.DataFrame | None = None,
    status: str = "ok",
    stage_id: str = "load",
) -> None:
    run = project_dir / "runs" / run_id
    (run / "outputs").mkdir(parents=True)
    rows = pd.DataFrame({"amount": _AMOUNTS, "memo": _MEMOS}) if frame is None else frame
    rows.to_parquet(run / "outputs" / f"{stage_id}.parquet", index=False)
    (run / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "started_at": run_id, "project": project_dir.name,
        "workflow_version": run_id, "human_review_queue_stats": {}, "status": "ok",
        "stage_records": [{
            "stage_id": stage_id, "type": "input_data", "status": status,
            "output_row_count": len(rows), "elapsed_ms": 1,
            "input_validation_report": [], "output_validation_report": None,
            "error": None, "output_path": f"outputs/{stage_id}.parquet",
        }],
    }), encoding="utf-8")


def _load(project_dir: Path, stage: Stage | None = None):
    reads = transform_input_schemas(stage or _stage())
    return load_stage_row_sources(project_dir.name, reads)["load"]


# ── which run the rows come from ─────────────────────────────────────────────

def test_the_newest_finished_run_supplies_the_rows(project_dir):
    _write_run(project_dir, "20260101T000000")
    _write_run(project_dir, "20260202T000000")

    assert _load(project_dir).run_id == "20260202T000000"


def test_a_run_whose_stage_errored_is_not_a_source(project_dir):
    """It wrote a frame too — of nulls where the stage never got to the column."""
    _write_run(project_dir, "20260101T000000")
    _write_run(project_dir, "20260202T000000", status="error")

    assert _load(project_dir).run_id == "20260101T000000"


def test_no_run_at_all_refuses_and_says_to_run_the_workflow(project_dir):
    with pytest.raises(NoRowsToSelectFrom, match="run the workflow first"):
        _load(project_dir)


def test_an_output_with_no_rows_is_not_a_selection_pool(project_dir):
    _write_run(project_dir, "20260101T000000",
               frame=pd.DataFrame({"amount": [], "memo": []}))

    with pytest.raises(NoRowsToSelectFrom, match="produced no rows"):
        _load(project_dir)


def test_a_read_column_the_run_never_wrote_refuses_naming_it(project_dir):
    _write_run(project_dir, "20260101T000000",
               frame=pd.DataFrame({"memo": _MEMOS}))

    with pytest.raises(NoRowsToSelectFrom, match="amount"):
        _load(project_dir)


# ── what the rows are narrowed to ────────────────────────────────────────────

def test_the_frame_holds_what_the_step_reads_and_nothing_else(project_dir):
    _write_run(project_dir, "20260101T000000")

    rows = _load(project_dir)
    assert list(rows.frame.columns) == ["amount"]
    assert rows.read_row(0) == {"amount": "30000.00"}


def test_a_blank_cell_reads_back_as_blank_not_as_the_word_none(project_dir):
    _write_run(project_dir, "20260101T000000")

    assert _load(project_dir).read_row(1) == {"amount": None}


def test_the_profile_counts_what_the_column_really_holds(project_dir):
    _write_run(project_dir, "20260101T000000")

    profile = _load(project_dir).profile
    assert profile.row_count == 5
    amount = profile.columns[0]
    assert amount.column == "amount" and amount.null_count == 1
    assert amount.distinct_count == 4


def test_a_row_number_outside_the_frame_refuses(project_dir):
    _write_run(project_dir, "20260101T000000")

    with pytest.raises(ValueError, match="rows 0 to 4"):
        _load(project_dir).read_row(99)


# ── searching them ───────────────────────────────────────────────────────────

def test_a_search_reports_what_it_matched_out_of_what_it_read(project_dir):
    _write_run(project_dir, "20260101T000000")

    matches = _load(project_dir).search("amount IS NULL")
    assert (matches.matched, matches.scanned) == (1, 5)
    assert [row.row for row in matches.rows] == [1]
    assert matches.rows[0].values == {"amount": None}


def test_a_filter_matching_most_of_the_frame_still_answers_with_the_count(project_dir):
    """The loose filter is the failure to design against, so the count is what shows it."""
    _write_run(project_dir, "20260101T000000")

    assert _load(project_dir).search("amount.str.contains('0')").matched == 4


def test_a_search_answers_with_at_most_one_readable_page(project_dir):
    _write_run(project_dir, "20260101T000000", frame=pd.DataFrame(
        {"amount": [f"{n}.00" for n in range(60)]}))

    matches = _load(project_dir).search("amount IS NOT NULL")
    assert matches.matched == 60 and len(matches.rows) == MAX_MATCHES


def test_a_filter_on_a_column_the_step_does_not_read_refuses(project_dir):
    _write_run(project_dir, "20260101T000000")

    with pytest.raises(ValueError, match="which this step does not"):
        _load(project_dir).search("memo.str.contains('Q1')")


def test_a_filter_outside_the_dialect_refuses(project_dir):
    _write_run(project_dir, "20260101T000000")

    with pytest.raises(PredicateError):
        _load(project_dir).search("evil(amount)")


def test_a_pattern_the_row_scan_cannot_run_refuses_before_it_is_run(project_dir):
    """A lookahead drops pandas off RE2 onto a backtracking engine — see issue #620."""
    _write_run(project_dir, "20260101T000000")

    with pytest.raises(ValueError, match="cannot be searched for"):
        _load(project_dir).search("amount.str.contains('(?=3)(3|3)*$')")
