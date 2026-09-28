"""End-to-end tests for the positional walk: clean chains, the stop cases, and
the defensive guards."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.core.errors import RowOutOfRange, StageNotInRun
from app.core.files import ProjectFile, delete_file, resolve_stored_path, save_upload
from app.core.frames import table_to_frame
from app.models import parse_stage
from app.runtime.branches import RowBranches
from app.runtime.lineage import single_parent_lineage
from app.runtime.stages.input_data import read_input_data
from app.runtime.trace import trace_row, trace_to_dict
from app.web.panel_links import AppPanelLinks, PacketPanelLinks
from app.web.trace_view import build_trace_view
from conftest import make_run_context, place_stage
from test_trace_helpers import write_run


def _chain(tmp_path, second_type: str):
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"], "name": ["A", "B", "C"]})
    enrich = seeds.assign(score=[10, 20, 30])
    return write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": second_type, "parents": ["seeds"], "df": enrich},
    ])


def test_row_preserving_chain_traces_to_origin(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    trace = trace_row(run_dir, "enrich", 1)
    assert [s.stage_id for s in trace.steps] == ["enrich", "seeds"]
    assert [s.row_ordinal for s in trace.steps] == [1, 1]         # same ordinal
    assert trace.steps[0].row["name"] == "B"
    assert trace.steps[0].columns_new == ["score"]               # new at enrich
    assert trace.steps[0].origin == "computed"
    assert trace.steps[1].columns_new == ["facility_id", "name"]  # origin: all new
    assert trace.end.reached_origin is True


def test_a_stage_that_recorded_only_its_branches_still_crosses(tmp_path):
    seeds = pd.DataFrame({"facility_id": ["a", "b"], "name": ["A", "B"]})
    # Branches ride in the lineage sidecar; no parents must not read as none.
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "tier", "type": "python_row_function", "parents": ["seeds"],
         "df": seeds.assign(tier=["high", "low"]),
         "branches": RowBranches([("transform/0:if",), ("transform/0:else",)])},
    ])

    trace = trace_row(run_dir, "tier", 1)
    assert [s.stage_id for s in trace.steps] == ["tier", "seeds"]
    assert trace.end.reached_origin is True


def test_llm_transform_traces_positionally(tmp_path):
    run_dir = _chain(tmp_path, "llm_transform")
    trace = trace_row(run_dir, "enrich", 1)
    assert [s.stage_id for s in trace.steps] == ["enrich", "seeds"]
    assert [s.row_ordinal for s in trace.steps] == [1, 1]         # same ordinal
    assert trace.steps[0].row["name"] == "B"
    assert trace.steps[0].columns_new == ["score"]               # new at enrich
    assert trace.steps[0].origin == "llm"
    assert trace.end.reached_origin is True


def test_human_review_queue_traces_positionally(tmp_path):
    run_dir = _chain(tmp_path, "human_review_queue")
    trace = trace_row(run_dir, "enrich", 1)
    assert [s.stage_id for s in trace.steps] == ["enrich", "seeds"]
    assert [s.row_ordinal for s in trace.steps] == [1, 1]         # same ordinal
    assert trace.steps[0].row["name"] == "B"
    assert trace.end.reached_origin is True


def test_a_frame_function_is_crossed_on_the_lineage_it_recorded(tmp_path):
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"], "name": ["A", "B", "C"]})
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "ranked", "type": "python_frame_function", "parents": ["seeds"],
         "df": seeds.iloc[[2, 0]].reset_index(drop=True),
         "lineage": single_parent_lineage("seeds", [2, 0])},
    ])
    trace = trace_row(run_dir, "ranked", 0)
    assert [(s.stage_id, s.row_ordinal) for s in trace.steps] == [("ranked", 0), ("seeds", 2)]
    assert trace.end.reached_origin is True


def test_rowcount_mismatch_on_preserving_stage_stops_defensively(tmp_path):
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"]})          # N = 3
    enrich = pd.DataFrame({"facility_id": ["a", "b"], "score": [1, 2]})  # M = 2 < N
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ])
    trace = trace_row(run_dir, "enrich", 0)
    assert [s.stage_id for s in trace.steps] == ["enrich"]          # enrich shown
    assert trace.end.reached_origin is False                       # but not to the origin
    assert "#58" in trace.end.message


def test_mismatch_deeper_in_chain_stops_at_the_right_step(tmp_path):
    a = pd.DataFrame({"k": ["a", "b", "c"]})                        # 3
    b = pd.DataFrame({"k": ["a", "b"], "x": [1, 2]})               # 2  (dropped one)
    c = pd.DataFrame({"k": ["a", "b"], "x": [1, 2], "y": [9, 8]})  # 2
    run_dir = write_run(tmp_path, [
        {"id": "a", "type": "input_data", "parents": [], "df": a},
        {"id": "b", "type": "python_row_function", "parents": ["a"], "df": b},
        {"id": "c", "type": "python_row_function", "parents": ["b"], "df": c},
    ])
    trace = trace_row(run_dir, "c", 0)
    assert [s.stage_id for s in trace.steps] == ["c", "b"]
    assert trace.end.reached_origin is False
    assert trace.end.at_stage == "b"


def test_row_out_of_range_raises(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    with pytest.raises(RowOutOfRange, match="out of range"):
        trace_row(run_dir, "enrich", 5)


def test_unknown_stage_raises(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    with pytest.raises(StageNotInRun, match="not in run"):
        trace_row(run_dir, "nope", 0)


def test_missing_output_file_stops(tmp_path):
    run_dir = _chain(tmp_path, "python_row_function")
    (run_dir / "outputs" / "seeds.parquet").unlink()
    trace = trace_row(run_dir, "enrich", 0)
    # 'enrich' shows, but crossing into 'seeds' finds no file.
    assert [s.stage_id for s in trace.steps] == ["enrich"]
    assert trace.end.reached_origin is False
    assert trace.end.at_stage == "seeds"


def test_preserving_stage_with_multiple_parents_stops(tmp_path):
    left = pd.DataFrame({"k": ["a", "b"]})
    right = pd.DataFrame({"k": ["a", "b"]})
    joined = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
    run_dir = write_run(tmp_path, [
        {"id": "left", "type": "input_data", "parents": [], "df": left},
        {"id": "right", "type": "input_data", "parents": [], "df": right},
        # Mislabeled as row-preserving but has two parents: not positional.
        {"id": "j", "type": "python_row_function", "parents": ["left", "right"], "df": joined},
    ])
    trace = trace_row(run_dir, "j", 0)
    assert [s.stage_id for s in trace.steps] == ["j"]
    assert trace.end.reached_origin is False


def _store_filings(tmp_path) -> ProjectFile:
    listed = tmp_path / "filings.csv"
    pd.DataFrame({"ecf_entry": [17, 58]}).to_csv(listed, index=False)
    with listed.open("rb") as stream:
        return save_upload("filings.csv", stream, project_id="boeing_docket")


def _docket_run(tmp_path) -> tuple[ProjectFile, Path]:
    """Filings read off a stored file, one row per page, and the pages put in date order."""
    record = _store_filings(tmp_path)
    read = read_input_data(place_stage(parse_stage({
        "id": "input_filings", "description": "input_filings", "type": "input_data",
        "connector": {"kind": "file", "params": {
            "paths": [str(resolve_stored_path(record))], "format": "csv"}},
        "signature": {"form": "replaces",
                      "produces": [{"name": "ecf_entry", "type": "int", "nullable": False}]},
    })), ctx=make_run_context())
    pages = pd.DataFrame({"ecf_entry": [17, 17, 58], "page": [1, 2, 1]})
    run_dir = write_run(tmp_path / "runs", [
        {"id": "input_filings", "type": "input_data", "parents": [],
         "df": table_to_frame(read.table), "lineage": read.lineage},
        {"id": "read_pages", "type": "read_pages", "parents": ["input_filings"],
         "df": pages, "lineage": single_parent_lineage("input_filings", [0, 0, 1])},
        {"id": "chronology", "type": "sort_rank", "parents": ["read_pages"],
         "df": pages.iloc[[2, 0, 1]].reset_index(drop=True),
         "lineage": single_parent_lineage("read_pages", [2, 0, 1])},
    ])
    return record, run_dir


def test_a_walk_from_a_chronology_row_ends_at_the_file_record_its_input_row_was_read_from(
    tmp_path,
):
    record, run_dir = _docket_run(tmp_path)

    steps = trace_to_dict(trace_row(run_dir, "chronology", 0))["steps"]

    assert [(step["stage_id"], step["row_ordinal"]) for step in steps] == [
        ("chronology", 0), ("read_pages", 2), ("input_filings", 1)]
    assert [step["source_id"] for step in steps] == [None, None, record.id]


def test_the_lineage_page_links_the_origin_row_to_its_file_page(tmp_path):
    record, run_dir = _docket_run(tmp_path)
    trace = trace_to_dict(trace_row(run_dir, "chronology", 0))

    served = build_trace_view(trace, {}, AppPanelLinks("boeing_docket", "T1"))
    packet = build_trace_view(trace, {}, PacketPanelLinks())

    assert served["nodes"][0]["source_file_href"] == f"/project/boeing_docket/files/{record.id}"
    # A packet is a folder with no route to serve the page.
    assert packet["nodes"][0]["source_file_href"] is None


def test_a_deleted_file_leaves_the_origin_row_unlinked(tmp_path):
    record, run_dir = _docket_run(tmp_path)
    delete_file("boeing_docket", record.id)

    view = build_trace_view(trace_to_dict(trace_row(run_dir, "chronology", 0)), {},
                            AppPanelLinks("boeing_docket", "T1"))
    assert view["nodes"][0]["source_file_href"] is None
