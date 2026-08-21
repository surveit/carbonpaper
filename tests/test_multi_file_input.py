"""One input stage reading several files of one shape, concatenated in bound order."""
from __future__ import annotations

from pathlib import PurePath

import pandas as pd
import pytest

from app.core.errors import FrameConcatMismatchError
from app.core.frames import table_to_frame
from app.models import Stage, parse_stage
from app.runtime.stages.input_data import preflight_input_data, read_input_data
from conftest import make_run_context, place_stage

_COLUMNS = [{"name": "month", "type": "str", "nullable": True},
            {"name": "reach", "type": "int", "nullable": True}]


def _stage(params: dict) -> Stage:
    return parse_stage({
        "id": "load", "description": "load", "type": "input_data",
        "connector": {"kind": "file", "params": params},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    })


def _write_csv(tmp_path, name: str, frame: pd.DataFrame) -> str:
    path = tmp_path / name
    frame.to_csv(path, index=False)
    return str(path)


def _three_months(tmp_path) -> list[str]:
    return [
        _write_csv(tmp_path, "jun.csv", pd.DataFrame({"month": ["jun"], "reach": [11]})),
        _write_csv(tmp_path, "jul.csv", pd.DataFrame({"month": ["jul"], "reach": [22]})),
        _write_csv(tmp_path, "aug.csv", pd.DataFrame({"month": ["aug"], "reach": [33]})),
    ]


def _read(params: dict) -> pd.DataFrame:
    output = read_input_data(place_stage(_stage(params)), ctx=make_run_context())
    return table_to_frame(output.table)


def test_several_files_are_read_as_one_table_in_the_order_they_were_bound(tmp_path):
    frame = _read({"paths": _three_months(tmp_path), "format": "csv"})
    assert list(frame["month"]) == ["jun", "jul", "aug"]
    assert list(frame["reach"]) == [11, 22, 33]


def test_binding_one_file_through_paths_reads_the_same_rows_as_binding_it_through_path(tmp_path):
    one = _write_csv(tmp_path, "jun.csv", pd.DataFrame({"month": ["jun"], "reach": [11]}))
    assert _read({"paths": [one], "format": "csv"}).equals(
        _read({"path": one, "format": "csv"}))


def test_a_file_missing_a_column_is_refused_rather_than_padded_with_nulls(tmp_path):
    paths = [
        _write_csv(tmp_path, "jun.csv", pd.DataFrame({"month": ["jun"], "reach": [11]})),
        _write_csv(tmp_path, "jul.csv", pd.DataFrame({"month": ["jul"]})),
    ]
    with pytest.raises(FrameConcatMismatchError) as refusal:
        _read({"paths": paths, "format": "csv"})
    # Named files, not "table 0" and "table 1" — the reader picked files, not tables.
    assert "'jul.csv'" in str(refusal.value) and "'jun.csv'" in str(refusal.value)
    assert "reach" in str(refusal.value)


def test_the_preflight_weighs_every_bound_file(tmp_path):
    issues, record = preflight_input_data(place_stage(_stage(
        {"paths": _three_months(tmp_path), "format": "csv"})))
    assert issues == []
    assert record is not None
    assert [PurePath(f["path"]).name for f in record["files"]] == ["jun.csv", "jul.csv", "aug.csv"]
    assert len({f["sha256"] for f in record["files"]}) == 3
    assert all(f["bytes"] > 0 for f in record["files"])


def test_one_bound_file_is_recorded_in_the_same_shape_as_several(tmp_path):
    one = _write_csv(tmp_path, "jun.csv", pd.DataFrame({"month": ["jun"], "reach": [11]}))
    _issues, record = preflight_input_data(place_stage(_stage({"paths": [one], "format": "csv"})))
    assert record is not None
    assert [f["path"] for f in record["files"]] == [one]


def test_every_missing_file_is_named_rather_than_only_the_first(tmp_path):
    present = _write_csv(tmp_path, "jun.csv", pd.DataFrame({"month": ["jun"], "reach": [11]}))
    issues, record = preflight_input_data(place_stage(_stage(
        {"paths": [present, str(tmp_path / "gone.csv"), str(tmp_path / "also-gone.csv")],
         "format": "csv"})))
    assert record is None
    assert len(issues) == 2
    assert "gone.csv" in issues[0] and "also-gone.csv" in issues[1]


def test_each_row_records_the_file_it_was_read_from(tmp_path):
    output = read_input_data(
        place_stage(_stage({"paths": _three_months(tmp_path), "format": "csv"})),
        ctx=make_run_context())
    assert output.lineage is not None
    origins = [entry[0] for entry in output.lineage.parents]
    assert [PurePath(p.source_file or "").name for p in origins] == [
        "jun.csv", "jul.csv", "aug.csv"]
    # Within its own file, so it is the row a reader would find by opening that file.
    assert [p.row_ordinal for p in origins] == [0, 0, 0]


def test_one_bound_file_records_its_name_too_rather_than_being_a_special_case(tmp_path):
    one = _write_csv(tmp_path, "jun.csv", pd.DataFrame({"month": ["jun"], "reach": [11]}))
    output = read_input_data(place_stage(_stage({"path": one, "format": "csv"})),
                             ctx=make_run_context())
    assert output.lineage is not None
    origin = output.lineage.parents[0][0]
    assert PurePath(origin.source_file or "").name == "jun.csv"
    assert origin.row_ordinal == 0


def test_the_trace_names_the_file_the_origin_row_was_read_from(tmp_path):
    from app.runtime.trace import trace_row
    from test_trace_helpers import write_run

    output = read_input_data(
        place_stage(_stage({"paths": _three_months(tmp_path), "format": "csv"})),
        ctx=make_run_context())
    run_dir = write_run(tmp_path / "runs", [{
        "id": "load", "type": "input_data", "parents": [],
        "df": table_to_frame(output.table), "lineage": output.lineage,
    }])

    step = trace_row(run_dir, "load", 1).steps[0]
    assert PurePath(step.source_file or "").name == "jul.csv"
    # Row 1 of the concatenation is row 0 of the file it came from.
    assert step.source_row == 0


def test_the_lineage_page_states_the_file_on_the_origin_row(tmp_path):
    from app.runtime.trace import trace_row, trace_to_dict
    from app.web.panel_links import AppPanelLinks
    from app.web.trace_view import build_trace_view
    from test_trace_helpers import write_run

    output = read_input_data(
        place_stage(_stage({"paths": _three_months(tmp_path), "format": "csv"})),
        ctx=make_run_context())
    run_dir = write_run(tmp_path / "runs", [{
        "id": "load", "type": "input_data", "parents": [],
        "df": table_to_frame(output.table), "lineage": output.lineage,
    }], input_bindings={"load": {"source": "run", "files": [
        {"path": path, "sha256": "0" * 64, "bytes": 1}
        for path in _three_months(tmp_path)]}})

    view = build_trace_view(
        trace_to_dict(trace_row(run_dir, "load", 2)), {},
        AppPanelLinks("demo", "T1"))
    origin = view["nodes"][0]
    # The name alone reaches the page; the absolute path stays in the manifest.
    assert origin["source_file"] == "aug.csv"
    assert "/" not in origin["source_file"]
    # Row 2 of the stage is row 0 of the third file, and the page says why they differ.
    assert (origin["source_row"], origin["row_ordinal"]) == (0, 2)
    assert origin["source_file_count"] == 3


def test_each_row_carries_the_sha_of_the_file_it_came_from(tmp_path):
    """A filename is what someone typed; the sha is what joins the row to the bytes."""
    output = read_input_data(
        place_stage(_stage({"paths": _three_months(tmp_path), "format": "csv"})),
        ctx=make_run_context())
    assert output.lineage is not None
    shas = [entry[0].source_file_sha for entry in output.lineage.parents]
    assert all(sha and len(sha) == 64 for sha in shas)
    assert len(set(shas)) == 3


def test_a_row_cap_on_a_source_stage_cuts_its_lineage_rather_than_moving_it(tmp_path):
    """A stage with no inputs originates its rows, so `--offset` cannot renumber them."""
    from app.runtime.executor import _RowWindow, _stage_row_lineage

    output = read_input_data(
        place_stage(_stage({"paths": _three_months(tmp_path), "format": "csv"})),
        ctx=make_run_context())
    windowed = _stage_row_lineage(
        place_stage(_stage({"paths": _three_months(tmp_path), "format": "csv"})),
        output, {}, _RowWindow(start=1, cap=1))

    assert windowed is not None and len(windowed) == 1
    # The second of the three files, at ITS row 0 — not row 0 shifted to row 1.
    origin = windowed.parents[0][0]
    assert PurePath(origin.source_file or "").name == "jul.csv"
    assert origin.row_ordinal == 0
