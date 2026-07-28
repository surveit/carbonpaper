"""The exporter writes each row's trace page into the published bundle, so a
copied folder keeps working with no app behind it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import TraceRowNotStamped, TraceUnavailableError
from app.runtime.trace_links import TRACE_ROW_ORDINAL_COLUMN, RowTraceExporter
from app.services.loader import load_workflow
from test_run_stage_views_pinned_version import LOAD_ID, _load_stage, _run_once

def at(row_ordinal: object) -> dict:
    """The runtime stamps this column onto the frame publish receives; these
    tests drive the exporter directly, so they stamp it themselves."""
    return {TRACE_ROW_ORDINAL_COLUMN: row_ordinal}


SCORE_ID = "score"
LABELS_ID = "labels"
MERGE_ID = "merge"

_COLUMNS = [{"name": "name", "type": "str"}, {"name": "val", "type": "int"}]
_SCORED = [*_COLUMNS, {"name": "score", "type": "int"}]
_LABELS = [{"name": "name", "type": "str"}, {"name": "label", "type": "str"}]


def _score_stage() -> dict:
    return {
        "id": SCORE_ID, "name": "Score rows", "type": "python_row_function",
        "inputs": [{"id": LOAD_ID, "schema": {"columns": _COLUMNS}}],
        "function": {"kind": "inline",
                     "code": 'def transform(row):\n    return {**row, "score": row["val"] * 2}\n'},
        "output_schema": {"columns": _SCORED},
    }


def _labels_stage(data_path: Path) -> dict:
    return {
        "id": LABELS_ID, "name": "Load labels", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(data_path), "format": "csv"}},
        "output_schema": {"columns": _LABELS},
    }


def _merge_stage() -> dict:
    """Two inputs: `trace_row` cannot cross fan-in on row position, so a row of
    this stage has no complete provenance chain (recorded lineage is issue #58)."""
    return {
        "id": MERGE_ID, "name": "Merge labels", "type": "join",
        "inputs": [{"id": SCORE_ID, "schema": {"columns": _SCORED}},
                   {"id": LABELS_ID, "schema": {"columns": _LABELS}}],
        "join": {"type": "inner", "keys": [{"left": "name", "right": "name"}]},
        "output_schema": {"columns": [*_SCORED, {"name": "label", "type": "str"}]},
    }


@pytest.fixture()
def exporter(tmp_path: Path) -> RowTraceExporter:
    project_dir = tmp_path / "trace_export"
    (project_dir / "compiled").mkdir(parents=True)
    data = project_dir / "rows.csv"
    data.write_text("name,val\na,1\nb,2\n", encoding="utf-8")
    (project_dir / "compiled" / "01_load.json").write_text(
        json.dumps(_load_stage(data)), encoding="utf-8")
    (project_dir / "compiled" / "02_score.json").write_text(
        json.dumps(_score_stage()), encoding="utf-8")
    labels = project_dir / "labels.csv"
    labels.write_text("name,label\na,first\nb,second\n", encoding="utf-8")
    (project_dir / "compiled" / "03_labels.json").write_text(
        json.dumps(_labels_stage(labels)), encoding="utf-8")
    (project_dir / "compiled" / "04_merge.json").write_text(
        json.dumps(_merge_stage()), encoding="utf-8")

    run_dir = project_dir / "runs" / _run_once(project_dir)
    output_dir = run_dir / "artifacts"
    output_dir.mkdir(parents=True, exist_ok=True)
    return RowTraceExporter(
        run_dir=run_dir,
        output_dir=output_dir,
        stages={stage.id: stage for stage in load_workflow(project_dir)},
    )


def test_writes_the_page_and_returns_a_relative_href(exporter):
    from_file = exporter.output_dir / "profiles" / "acme.html"
    from_file.parent.mkdir(parents=True, exist_ok=True)
    href = exporter.export_row_trace(SCORE_ID, from_file, row=at(0))
    assert href == "../_traces/score/0.html"
    assert (from_file.parent / href).resolve().is_file()


def test_the_written_page_makes_no_absolute_requests(exporter):
    from_file = exporter.output_dir / "index.html"
    href = exporter.export_row_trace(SCORE_ID, from_file, row=at(0))
    html = (from_file.parent / href).resolve().read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html


def test_assets_land_beside_the_pages(exporter):
    exporter.export_row_trace(SCORE_ID, exporter.output_dir / "index.html", row=at(0))
    assert (exporter.output_dir / "_assets/trace.css").is_file()


def test_the_page_reaches_its_assets_from_its_own_depth(exporter):
    href = exporter.export_row_trace(SCORE_ID, exporter.output_dir / "index.html", row=at(0))
    page = (exporter.output_dir / href).resolve()
    prefix = page.read_text(encoding="utf-8").split('href="', 1)[1].split("style.css", 1)[0]
    assert (page.parent / (prefix + "style.css")).resolve().is_file()


def test_raises_rather_than_returning_a_dead_link(exporter):
    with pytest.raises(TraceUnavailableError) as excinfo:
        exporter.export_row_trace(SCORE_ID, exporter.output_dir / "index.html", row=at(9999))
    assert "9999" in str(excinfo.value)


def test_raises_for_a_row_whose_lineage_crosses_a_fan_in(exporter):
    """The row exists and renders fine; what it lacks is a chain reaching the
    source. An href here would advertise provenance the page cannot show."""
    with pytest.raises(TraceUnavailableError) as excinfo:
        exporter.export_row_trace(MERGE_ID, exporter.output_dir / "index.html", row=at(0))
    assert MERGE_ID in str(excinfo.value)
    assert not (exporter.output_dir / "_traces" / MERGE_ID).exists()


def test_rejects_a_from_file_outside_the_bundle(exporter, tmp_path):
    """An href from outside the bundle climbs out of it: it resolves here and
    breaks the moment the bundle is copied."""
    outside = tmp_path / "elsewhere" / "index.html"
    with pytest.raises(ValueError) as excinfo:
        exporter.export_row_trace(SCORE_ID, outside, row=at(0))
    assert "elsewhere" in str(excinfo.value) and str(exporter.output_dir) in str(excinfo.value)


def test_rejects_a_row_that_carries_no_ordinal(exporter):
    """A row the author built by hand, or one whose stamp was dropped, carries
    no ordinal to read. No page is written."""
    with pytest.raises(TraceRowNotStamped) as excinfo:
        exporter.export_row_trace(
            SCORE_ID, exporter.output_dir / "index.html", row={"name": "a"})
    assert TRACE_ROW_ORDINAL_COLUMN in str(excinfo.value)
    assert not (exporter.output_dir / "_traces").exists()


def test_rejects_a_mangled_stamp(exporter):
    """A stamp overwritten with a negative, a string, or a null is not a
    position the runtime wrote, so it is refused rather than coerced."""
    for mangled in (-1, "0", None):
        with pytest.raises(TraceRowNotStamped):
            exporter.export_row_trace(
                SCORE_ID, exporter.output_dir / "index.html", row=at(mangled))


def test_a_second_call_reuses_the_page_instead_of_rewriting_it(exporter):
    """Many published rows can point at one trace; writing it per call would
    rewrite the same file hundreds of times."""
    from_file = exporter.output_dir / "index.html"
    a = exporter.export_row_trace(SCORE_ID, from_file, row=at(0))
    page = (from_file.parent / a).resolve()

    # A sentinel survives only if the second call does not rewrite the file.
    page.write_text(page.read_text(encoding="utf-8") + "<!--sentinel-->", encoding="utf-8")
    b = exporter.export_row_trace(SCORE_ID, from_file, row=at(0))

    assert b == a
    assert "<!--sentinel-->" in page.read_text(encoding="utf-8")
