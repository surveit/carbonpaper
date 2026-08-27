"""A report stage asserts where a value came from; the provider checks the cell."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import CitationMismatch, RowOutOfRange, StageNotInRun
from app.models import parse_stage, Stage
from app.models.citations import CitedValue
from app.models.claims import StageOutputRowCitation
from app.runtime.citations import CitationProvider
from app.runtime.context import RunContext
from app.runtime.stages.report import handle_report
from conftest import as_inputs, place_stage

_IN_HOUSE = pd.DataFrame([{
    "in_house_mentions": 24,
    "in_house_income_usd": 0.0,
    "in_house_expenses_usd": 10333414.94,
}])
_CLIENTS = pd.DataFrame([{"clients_paying": 24, "external_spend": 4461000.0}])

_FRAMES = {"count_in_house_figures": _IN_HOUSE, "count_client_figures": _CLIENTS}


def _provider(frames=None) -> CitationProvider:
    return CitationProvider(
        project="venezuela_lda_lobbying", run_id="R1",
        tables=as_inputs(_FRAMES if frames is None else frames),
    )


# ── citing a value ────────────────────────────────────────────────────────────

def test_a_value_the_cell_holds_is_cited_and_gets_that_cell_s_trace_url():
    url = _provider().cite_value(
        "count_client_figures", 0, "external_spend", 4461000.0,
        label="Total external spend",
    )
    assert url == (
        "/project/venezuela_lda_lobbying/runs/R1"
        "/stage/count_client_figures/row/0/trace/view?column=external_spend"
    )


def test_a_citation_records_the_cell_the_name_and_where_it_sits():
    provider = _provider()
    provider.cite_value(
        "count_client_figures", 0, "external_spend", 4461000.0,
        label="Total external spend",
    )
    assert provider.citations == [CitedValue(
        stage_id="count_client_figures", row_ordinal=0, column="external_spend",
        label="Total external spend", value="4461000.0",
    )]


def test_a_sum_of_two_cells_is_refused_by_the_cell_it_claims():
    # in_house_income_usd + in_house_expenses_usd, added in the report.
    total = _IN_HOUSE["in_house_income_usd"].iloc[0] + _IN_HOUSE["in_house_expenses_usd"].iloc[0]
    with pytest.raises(CitationMismatch) as exc:
        _provider().cite_value(
            "count_in_house_figures", 0, "in_house_mentions", total,
            label="In-house money excluded from every total",
        )
    assert "In-house money excluded from every total" in str(exc.value)


def test_typesetting_the_cell_instead_of_passing_it_is_refused():
    with pytest.raises(CitationMismatch) as exc:
        _provider().cite_value(
            "count_client_figures", 0, "external_spend", "$4,461,000",
            label="Total external spend",
        )
    assert "4461000.0" in str(exc.value)


def test_cite_value_refuses_a_stage_this_report_stage_was_not_given():
    with pytest.raises(StageNotInRun) as exc:
        _provider().cite_value("spend_by_firm", 0, "total_income_usd", 1.0, label="Firms")
    assert "spend_by_firm" in str(exc.value)


def test_cite_value_refuses_a_row_the_frame_does_not_have():
    with pytest.raises(RowOutOfRange):
        _provider().cite_value("count_client_figures", 3, "clients_paying", 24, label="C")


def test_cite_value_refuses_a_column_the_frame_does_not_have():
    with pytest.raises(ValueError) as exc:
        _provider().cite_value("count_client_figures", 0, "external_spent", 1.0, label="S")
    assert "external_spent" in str(exc.value)


def test_a_null_cell_is_cited_as_absent_not_as_the_word_nan():
    provider = _provider({"counts": pd.DataFrame([{"judgement_filings": None}])})
    provider.cite_value("counts", 0, "judgement_filings", None, label="Judgement filings")
    assert provider.citations[0].value == ""


# ── citing a row, with no value ───────────────────────────────────────────────

def test_cite_row_claims_the_row_and_no_value():
    provider = _provider()
    url = provider.cite_row("count_in_house_figures", 0)
    assert url.endswith("/stage/count_in_house_figures/row/0/trace/view")
    assert provider.cited_rows == [
        StageOutputRowCitation(stage_id="count_in_house_figures", row_ordinal=0)
    ]
    assert provider.citations == []


def test_cite_row_refuses_a_row_the_frame_does_not_have():
    with pytest.raises(RowOutOfRange):
        _provider().cite_row("count_client_figures", 7)


# ── through the handler ───────────────────────────────────────────────────────

_CITES_A_FIGURE = """
import pathlib

def transform(counts, output_dir, citation_provider):
    spend = counts["in_house_expenses_usd"].iloc[0]
    url = citation_provider.cite_value(
        "count_in_house_figures", 0, "in_house_expenses_usd", spend,
        label="In-house money excluded from every total",
    )
    path = pathlib.Path(output_dir) / "index.html"
    path.write_text(
        "<a href='" + url + "'>$" + format(spend, ",.2f") + "</a>", encoding="utf-8"
    )
    return pd.DataFrame({"path": [str(path)]})
"""

_SUMS_TWO_CELLS = _CITES_A_FIGURE.replace(
    'spend = counts["in_house_expenses_usd"].iloc[0]',
    'spend = counts["in_house_income_usd"].iloc[0] + counts["in_house_mentions"].iloc[0]',
)


def _report_stage(code: str) -> Stage:
    return parse_stage({
        "id": "publish_venezuela_workbook",
        "type": "report",
        "description": "Publish the workbook",
        "inputs": [{"id": "count_in_house_figures"}],
        "report": {"format": "html_report", "destination": "build/"},
        "signature": {"form": "replaces"},
        "function": {"kind": "inline", "code": "import pandas as pd\n" + code},
    })


def _run_publish(code: str, tmp_path):
    ctx = RunContext.for_workflow_run(
        run_dir=tmp_path / "run",
        project_id="venezuela_lda_lobbying", run_id="R1",
    )
    return handle_report(
        place_stage(_report_stage(code)), as_inputs({"count_in_house_figures": _IN_HOUSE}), ctx
    )


def test_a_cited_figure_publishes(tmp_path):
    _run_publish(_CITES_A_FIGURE, tmp_path)
    html = (tmp_path / "run" / "artifacts" / "build" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "count_in_house_figures/row/0/trace/view" in html
    assert "$10,333,414.94" in html


def test_a_figure_computed_in_the_report_stops_the_stage(tmp_path):
    with pytest.raises(CitationMismatch):
        _run_publish(_SUMS_TWO_CELLS, tmp_path)
