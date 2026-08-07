from __future__ import annotations

import json

import pytest

from app.services.review_packet.notebook import CAVEAT, build_run_notebook
from app.services.review_packet.views import RunView, StageView


_UNSET = object()


def _stage(stage_id="load", stype="input_data", definition=None, data_file=_UNSET):
    resolved = f"data/{stage_id}.csv" if data_file is _UNSET else data_file
    return StageView(
        record={}, stage_id=stage_id, type=stype, status="ok", row_count=12,
        elapsed_ms=34, error=None, notes=[], output_path=f"outputs/{stage_id}.parquet",
        validations=[], data_file=resolved,
        definition=definition, definition_error=None,
    )


def _view(stages):
    return RunView(
        project="p", run_id="r", status="ok", started_at="2026-01-01T00:00:00",
        finished_at=None, workflow_version="v1", is_test_run=False, bust_cache=False,
        halted_at=[], dropped_columns={}, stages=stages, inputs=[],
    )


def _cells(view):
    return json.loads(build_run_notebook(view))["cells"]


def _source(cell):
    return "".join(cell["source"])


def test_two_dumps_of_one_run_are_byte_identical(sample_run_view):
    # Hashed cell ids, not nbformat's random ones, so the file can be checksummed.
    assert build_run_notebook(sample_run_view) == build_run_notebook(sample_run_view)


def test_the_caveat_is_the_first_thing_the_reader_meets(sample_run_view):
    first = _cells(sample_run_view)[0]

    assert first["cell_type"] == "markdown"
    assert CAVEAT in _source(first)


def test_a_step_without_authored_code_gets_no_code_written_for_it(sample_run_view):
    # The step ran a spec, so invented pandas would show code that never executed.
    sources = [_source(c) for c in _cells(sample_run_view) if c["cell_type"] == "code"]

    assert not any("groupby" in s or "merge" in s for s in sources)


def test_every_step_loads_the_output_this_run_wrote(sample_run_view):
    loads = [
        _source(c) for c in _cells(sample_run_view)
        if c["cell_type"] == "code" and "read_csv" in _source(c)
    ]

    assert len(loads) == len(sample_run_view.stages)
    assert all("{DATA}/" in s for s in loads)


def test_a_step_whose_id_is_a_python_keyword_still_yields_valid_code():
    # `class` is snake_case, so the id validator permits it; binding it would not parse.
    view = _view([_stage(stage_id="class")])

    loads = [c for c in _cells(view) if "read_csv" in _source(c)]

    compile(_source(loads[0]), "<cell>", "exec")
    assert "frames['class']" in _source(loads[0])


def test_every_code_cell_parses_as_python(sample_run_view):
    for cell in _cells(sample_run_view):
        if cell["cell_type"] == "code":
            compile(_source(cell), "<cell>", "exec")


def test_the_notebook_carries_no_stale_execution_state(sample_run_view):
    for cell in _cells(sample_run_view):
        if cell["cell_type"] == "code":
            assert cell["execution_count"] is None
            assert cell["outputs"] == []


def test_cell_ids_are_unique(sample_run_view):
    ids = [c["id"] for c in _cells(sample_run_view)]

    assert len(ids) == len(set(ids))


def test_a_step_with_no_output_file_gets_no_load_cell():
    view = _view([_stage(stage_id="publish_it", stype="publish", data_file=None)])

    assert not [c for c in _cells(view) if "read_csv" in _source(c)]


@pytest.fixture
def sample_run_view():
    return _view([
        _stage(stage_id="read_filings"),
        _stage(stage_id="join_names", stype="enrich"),
        _stage(stage_id="count_by_firm", stype="aggregate"),
    ])
