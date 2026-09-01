"""A sandboxed report writes only through its builtins, and cites what it prints."""
from __future__ import annotations

import csv

import pandas as pd
import pytest

from app.core.errors import CitationMismatch, TraceLinksUnavailableError
from app.models import parse_stage, Stage
from app.runtime.context import RunContext
from app.runtime.stages.starlark_report import handle_starlark_report
from conftest import as_inputs, place_stage, rows_of

_IN_HOUSE = pd.DataFrame([{
    "in_house_mentions": 24,
    "in_house_income_usd": 0.0,
    "in_house_expenses_usd": 10333414.94,
}])

_INPUT = {"count_in_house_figures": _IN_HOUSE}


def _stage(code: str) -> Stage:
    return parse_stage({
        "id": "write_report", "description": "Write the lobbying report",
        "type": "starlark_report",
        "inputs": [{"id": "count_in_house_figures"}],
        "signature": {"form": "replaces"},
        "starlark_report": {
            "summary": "Writes the in-house lobbying spend out as one page.",
            "corner_cases": [],
            "code": code,
        },
    })


def _run(code: str, tmp_path, *, project_id: str | None = "venezuela_lda_lobbying"):
    ctx = (
        RunContext.for_workflow_run(run_dir=tmp_path / "run", project_id=project_id, run_id="R1")
        if project_id is not None
        else RunContext.for_stages_outside_a_run(run_dir=tmp_path / "run")
    )
    return handle_starlark_report(place_stage(_stage(code)), as_inputs(_INPUT), ctx)


def _artifact(tmp_path, name: str) -> str:
    return (tmp_path / "run" / "artifacts" / "build" / name).read_text(encoding="utf-8")


_CITES_A_FIGURE = '''
def transform(figures):
    spend = figures[0]["in_house_expenses_usd"]
    url = cite_value("count_in_house_figures", 0, "in_house_expenses_usd", spend,
                     label="In-house lobbying spend")
    emit_file("index.html", '<h1><a href="%s">$%s</a></h1>' % (url, format_number(spend)))
'''

_SUMS_TWO_CELLS = '''
def transform(figures):
    total = figures[0]["in_house_income_usd"] + figures[0]["in_house_expenses_usd"]
    url = cite_value("count_in_house_figures", 0, "in_house_mentions", total,
                     label="In-house money excluded from every total")
    emit_file("index.html", url)
'''


def test_a_cited_figure_is_written_beside_the_link_to_its_row(tmp_path):
    _run(_CITES_A_FIGURE, tmp_path)
    page = _artifact(tmp_path, "index.html")
    assert "count_in_house_figures/row/0/trace/view" in page
    assert "$10,333,414.94" in page


def test_a_figure_the_report_computed_itself_stops_the_stage(tmp_path):
    with pytest.raises(CitationMismatch):
        _run(_SUMS_TWO_CELLS, tmp_path)


def test_the_stage_output_lists_what_was_written(tmp_path):
    output = _run(_CITES_A_FIGURE, tmp_path)
    assert list(rows_of(output)["file"]) == ["index.html"]


def test_a_name_that_climbs_out_of_the_output_directory_is_refused(tmp_path):
    code = 'def transform(figures):\n    emit_file("../../escaped.html", "x")\n'
    with pytest.raises(ValueError) as exc:
        _run(code, tmp_path)
    assert "plain relative filename" in str(exc.value)
    assert not (tmp_path / "run" / "escaped.html").exists()


def test_an_absolute_name_is_refused(tmp_path):
    code = f'def transform(figures):\n    emit_file("{tmp_path}/escaped.html", "x")\n'
    with pytest.raises(ValueError):
        _run(code, tmp_path)
    assert not (tmp_path / "escaped.html").exists()


def test_emit_table_writes_the_rows_as_csv(tmp_path):
    code = (
        'def transform(figures):\n'
        '    emit_table("figures.csv", [{"mentions": r["in_house_mentions"],\n'
        '                                "spend": r["in_house_expenses_usd"]}\n'
        '                               for r in figures])\n'
    )
    _run(code, tmp_path)
    written = list(csv.DictReader(_artifact(tmp_path, "figures.csv").splitlines()))
    assert written == [{"mentions": "24", "spend": "10333414.94"}]


def test_a_row_naming_different_columns_stops_the_stage(tmp_path):
    code = (
        'def transform(figures):\n'
        '    emit_table("figures.csv", [{"mentions": 24}, {"spend": 10333414.94}])\n'
    )
    with pytest.raises(ValueError) as exc:
        _run(code, tmp_path)
    assert "row 1 carries ['spend']" in str(exc.value)


def test_escape_puts_a_cell_inside_markup_safely(tmp_path):
    code = (
        'def transform(figures):\n'
        '    emit_file("index.html", "<p>%s</p>" % escape("Baker & McKenzie <LLP>"))\n'
    )
    _run(code, tmp_path)
    assert "Baker &amp; McKenzie &lt;LLP&gt;" in _artifact(tmp_path, "index.html")


# ── a run with no project scope ───────────────────────────────────────────────

def test_a_scopeless_run_still_writes_a_report_that_cites_nothing(tmp_path):
    code = 'def transform(figures):\n    emit_file("index.html", "<p>no citations</p>")\n'
    _run(code, tmp_path, project_id=None)
    assert "no citations" in _artifact(tmp_path, "index.html")


def test_a_scopeless_run_fails_only_where_the_code_cites(tmp_path):
    with pytest.raises(TraceLinksUnavailableError):
        _run(_CITES_A_FIGURE, tmp_path, project_id=None)


# ── printing a number ─────────────────────────────────────────────────────────

def test_a_float_goes_to_the_page_with_its_digits_intact(tmp_path):
    code = (
        'def transform(figures):\n'
        '    spend = figures[0]["in_house_expenses_usd"]\n'
        '    emit_file("plain.txt", "%s vs %s" % (spend, format_number(spend)))\n'
    )
    _run(code, tmp_path)
    # Starlark's own rendering rounds to 7 significant figures; the builtin does not.
    assert _artifact(tmp_path, "plain.txt") == "1.033341e+07 vs 10,333,414.94"


def test_a_blank_cell_is_refused_rather_than_read_as_a_number(tmp_path):
    code = (
        'def transform(figures):\n'
        '    emit_file("plain.txt", format_number(figures[0].get("missing_figure")))\n'
    )
    with pytest.raises(ValueError) as exc:
        _run(code, tmp_path)
    assert "say what an absent figure should read as" in str(exc.value)
