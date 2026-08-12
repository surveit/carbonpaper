"""A publish stage copies: it reads a figure off a row it was given, and the run
refuses a printed number that row does not hold.

The rows here are the counting stages of venezuela_lda_lobbying run
20260812T133317.816579, and the labels are what its workbook prints.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.models import parse_stage, Stage
from app.models.errors import StepRefused
from app.runtime.context import RunContext
from app.runtime.published_figures import find_published_figure_issues
from app.runtime.stages.publish import handle_publish
from app.runtime.trace_links import RowTraceLinker, RowTraceTarget
from conftest import place_stage

_IN_HOUSE = pd.DataFrame([{
    "in_house_mentions": 24,
    "in_house_income_usd": 0.0,
    "in_house_expenses_usd": 10333414.94,
}])
_CLIENTS = pd.DataFrame([{"clients_paying": 24, "external_spend": 4461000.0}])

_FRAMES = {"count_in_house_figures": _IN_HOUSE, "count_client_figures": _CLIENTS}


def _linker(frames=None) -> RowTraceLinker:
    return RowTraceLinker(
        project="venezuela_lda_lobbying", run_id="R1",
        frames=_FRAMES if frames is None else frames,
    )


# ── the accessor: one call, value and trace together ──────────────────────────

def test_read_figure_hands_back_the_cell_and_the_row_it_came_from():
    figure = _linker().read_figure(
        "count_client_figures", 0, "external_spend", label="Total external spend"
    )
    assert figure.value == 4461000.0
    assert figure.label == "Total external spend"
    assert figure.url == (
        "/project/venezuela_lda_lobbying/runs/R1"
        "/stage/count_client_figures/row/0/trace/view"
    )


def test_read_figure_records_the_label_and_the_cell_it_read():
    linker = _linker()
    linker.read_figure("count_client_figures", 0, "clients_paying", label="Clients paying")
    assert linker.issued == [RowTraceTarget(
        stage_id="count_client_figures", row_ordinal=0,
        label="Clients paying", value="24",
    )]


def test_read_figure_refuses_a_stage_this_publish_stage_was_not_given():
    with pytest.raises(StepRefused) as exc:
        _linker().read_figure("spend_by_firm", 0, "total_income_usd", label="Firm spend")
    assert "spend_by_firm" in str(exc.value)


def test_read_figure_refuses_a_row_the_frame_does_not_have():
    with pytest.raises(StepRefused) as exc:
        _linker().read_figure("count_client_figures", 3, "clients_paying", label="Clients")
    assert "1 rows" in str(exc.value)


def test_read_figure_refuses_a_column_the_frame_does_not_have():
    with pytest.raises(StepRefused) as exc:
        _linker().read_figure("count_client_figures", 0, "external_spent", label="Spend")
    assert "external_spent" in str(exc.value)


# ── the check: what a figure printed is in the row it named ───────────────────

def _target(stage_id: str, ordinal: int, label: str, value: str | None) -> RowTraceTarget:
    return RowTraceTarget(
        stage_id=stage_id, row_ordinal=ordinal, label=label, value=value
    )


def test_a_figure_read_off_the_row_passes():
    linker = _linker()
    linker.read_figure("count_in_house_figures", 0, "in_house_mentions", label="Mentions")
    assert find_published_figure_issues(linker.issued, _FRAMES) == []


def test_formatting_a_cell_for_a_reader_passes():
    issued = [_target("count_client_figures", 0, "Total external spend", "$4,461,000")]
    assert find_published_figure_issues(issued, _FRAMES) == []


def test_a_figure_that_adds_two_cells_of_the_row_is_refused():
    # in_house_income_usd + in_house_expenses_usd, summed in publish.
    issued = [_target(
        "count_in_house_figures", 0, "In-house money excluded", "$10,333,415"
    )]
    assert find_published_figure_issues(issued, _FRAMES) == [
        "'In-house money excluded' prints '$10,333,415', which is in no cell of "
        "'count_in_house_figures' row 0"
    ]


def test_a_trace_naming_a_stage_this_publish_stage_was_not_given_is_refused():
    issued = [_target("spend_by_firm", 0, "Firms paid", "14")]
    assert "not an input" in find_published_figure_issues(issued, _FRAMES)[0]


def test_a_trace_naming_a_row_the_frame_does_not_have_is_refused():
    issued = [_target("count_client_figures", 7, "Clients paying", "24")]
    assert "has 1 rows" in find_published_figure_issues(issued, _FRAMES)[0]


def test_a_row_link_that_claims_no_value_is_checked_only_for_the_row():
    assert find_published_figure_issues(
        [_target("count_client_figures", 0, "Show the work", None)], _FRAMES
    ) == []


# ── through the handler ───────────────────────────────────────────────────────

_READS_A_FIGURE = """
import pathlib

def transform(counts, output_dir, trace_links):
    figure = trace_links.read_figure(
        "count_in_house_figures", 0, "in_house_expenses_usd",
        label="In-house money excluded from every total",
    )
    path = pathlib.Path(output_dir) / "index.html"
    path.write_text(
        "<a href='" + figure.url + "'>$" + format(figure.value, ",.2f") + "</a>",
        encoding="utf-8",
    )
    return pd.DataFrame({"path": [str(path)]})
"""

_SUMS_TWO_CELLS = """
import pathlib

def transform(counts, output_dir, trace_links):
    total = counts["in_house_income_usd"].iloc[0] + counts["in_house_expenses_usd"].iloc[0]
    printed = "$" + format(total, ",.0f")
    url = trace_links.build_row_trace_url(
        "count_in_house_figures", 0,
        label="In-house money excluded from every total", value=printed,
    )
    path = pathlib.Path(output_dir) / "index.html"
    path.write_text("<a href='" + url + "'>" + printed + "</a>", encoding="utf-8")
    return pd.DataFrame({"path": [str(path)]})
"""


def _publish_stage(code: str) -> Stage:
    return parse_stage({
        "id": "publish_venezuela_workbook",
        "type": "publish",
        "description": "Publish the workbook",
        "inputs": [{"id": "count_in_house_figures"}],
        "publish": {"format": "html_report", "destination": "build/"},
        "signature": {"form": "replaces"},
        "function": {"kind": "inline", "code": "import pandas as pd\n" + code},
    })


def _run_publish(code: str, tmp_path):
    ctx = RunContext.for_workflow_run(
        repo_root=tmp_path, run_dir=tmp_path / "run",
        project="venezuela_lda_lobbying", run_id="R1",
    )
    return handle_publish(
        place_stage(_publish_stage(code)), {"count_in_house_figures": _IN_HOUSE}, ctx
    )


def test_a_figure_read_off_the_row_publishes(tmp_path):
    _run_publish(_READS_A_FIGURE, tmp_path)
    html = (tmp_path / "run" / "artifacts" / "build" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "count_in_house_figures/row/0/trace/view" in html
    assert "$10,333,414.94" in html


def test_a_figure_summed_in_publish_stops_the_stage(tmp_path):
    with pytest.raises(StepRefused) as exc:
        _run_publish(_SUMS_TWO_CELLS, tmp_path)
    assert "$10,333,415" in str(exc.value)
    assert "belongs in a stage ahead of publish" in str(exc.value)
