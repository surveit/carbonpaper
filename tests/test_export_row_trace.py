"""The exporter writes each row's trace page into the published bundle, so a
copied folder keeps working with no app behind it."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import TraceUnavailableError
from app.runtime.trace_links import RowTraceExporter
from app.services.loader import load_workflow
from test_run_stage_views_pinned_version import LOAD_ID, _load_stage, _run_once

SCORE_ID = "score"

_COLUMNS = [{"name": "name", "type": "str"}, {"name": "val", "type": "int"}]


def _score_stage() -> dict:
    return {
        "id": SCORE_ID, "name": "Score rows", "type": "python_row_function",
        "inputs": [{"id": LOAD_ID, "schema": {"columns": _COLUMNS}}],
        "function": {"kind": "inline",
                     "code": 'def transform(row):\n    return {**row, "score": row["val"] * 2}\n'},
        "output_schema": {"columns": [*_COLUMNS, {"name": "score", "type": "int"}]},
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
    href = exporter.export_row_trace(SCORE_ID, 0, from_file)
    assert href == "../_traces/score/0.html"
    assert (from_file.parent / href).resolve().is_file()


def test_the_written_page_makes_no_absolute_requests(exporter):
    from_file = exporter.output_dir / "index.html"
    href = exporter.export_row_trace(SCORE_ID, 0, from_file)
    html = (from_file.parent / href).resolve().read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html


def test_assets_land_beside_the_pages(exporter):
    exporter.export_row_trace(SCORE_ID, 0, exporter.output_dir / "index.html")
    assert (exporter.output_dir / "_assets/mermaid.min.js").is_file()


def test_the_page_reaches_its_assets_from_its_own_depth(exporter):
    href = exporter.export_row_trace(SCORE_ID, 0, exporter.output_dir / "index.html")
    page = (exporter.output_dir / href).resolve()
    prefix = page.read_text(encoding="utf-8").split('href="', 1)[1].split("style.css", 1)[0]
    assert (page.parent / (prefix + "style.css")).resolve().is_file()


def test_raises_rather_than_returning_a_dead_link(exporter):
    with pytest.raises(TraceUnavailableError) as excinfo:
        exporter.export_row_trace(SCORE_ID, 9999, exporter.output_dir / "index.html")
    assert "9999" in str(excinfo.value)


def test_rejects_a_negative_ordinal(exporter):
    with pytest.raises(ValueError):
        exporter.export_row_trace(SCORE_ID, -1, exporter.output_dir / "index.html")


def test_a_second_call_reuses_the_page_instead_of_rewriting_it(exporter):
    """Many published rows can point at one trace; writing it per call would
    rewrite the same file hundreds of times."""
    from_file = exporter.output_dir / "index.html"
    a = exporter.export_row_trace(SCORE_ID, 0, from_file)
    page = (from_file.parent / a).resolve()

    # A sentinel survives only if the second call does not rewrite the file.
    page.write_text(page.read_text(encoding="utf-8") + "<!--sentinel-->", encoding="utf-8")
    b = exporter.export_row_trace(SCORE_ID, 0, from_file)

    assert b == a
    assert "<!--sentinel-->" in page.read_text(encoding="utf-8")
