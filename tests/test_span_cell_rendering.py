"""A span cell renders as its quote and the page it sits on, linked, in every table of a run."""
from __future__ import annotations

import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.locators import CellAt, PageCharRange
from app.models.spans import Span
from app.web.config import templates
from app.web.panel_links import AppPanelLinks, PacketPanelLinks
from app.web.review_packet import export_review_packet
from app.web.span_cells import SpanCellText
from run_seed import store_manifest

PROJECT = "docket"
RUN = "run-0001"
STAGE = "claims"
OPPOSE = Span(source_id="ecf17", source_sha256="a" * 64,
              locator=PageCharRange(page=14, start=120, end=126), quote="oppose")
DISMISS = Span(source_id="ecf17", source_sha256="a" * 64,
               locator=PageCharRange(page=2, start=4, end=11), quote="dismiss")
OPPOSE_HREF = f"/project/{PROJECT}/files/ecf17?page=14&amp;start=120&amp;end=126"
DISMISS_HREF = f"/project/{PROJECT}/files/ecf17?page=2&amp;start=4&amp;end=11"
OPPOSE_CITE = f'<q title="oppose">oppose</q> <a href="{OPPOSE_HREF}">page 14</a>'
DISMISS_CITE = f'<q title="dismiss">dismiss</q> <a href="{DISMISS_HREF}">page 2</a>'
# Row 1 holds a null span and a null list: a struct column with a null row is where
# pandas turns the page and offsets to floats, so these rows are what keep them ints.
CLAIMS = pa.table({
    "claim": ["The families oppose the motion.", "No quote was found."],
    "quote": pa.array([OPPOSE.model_dump(), None]),
    "quotes": pa.array([[OPPOSE.model_dump(), DISMISS.model_dump()], None]),
})


@pytest.fixture
def run_dir(projects_root: Path) -> Path:
    run_dir = projects_root / PROJECT / "runs" / RUN
    (run_dir / "outputs").mkdir(parents=True)
    pq.write_table(CLAIMS, run_dir / "outputs" / f"{STAGE}.parquet")
    store_manifest(projects_root / PROJECT, RUN, {
        "kind": "runs", "run_id": RUN, "started_at": RUN, "project": PROJECT,
        "workflow_version": RUN, "status": "ok", "human_review_queue_stats": {},
        "stage_records": [{
            "stage_id": STAGE, "type": "input_data", "description": STAGE, "status": "ok",
            "elapsed_ms": 5,
            "input_validation_report": [], "output_validation_report": None,
            "output_row_count": CLAIMS.num_rows, "output_path": f"outputs/{STAGE}.parquet",
        }],
    })
    return run_dir


def _get(path: str) -> str:
    response = TestClient(app).get(f"/project/{PROJECT}/runs/{RUN}/stage/{STAGE}{path}")
    assert response.status_code == 200, response.text
    return response.text


def _cells_of_row(html: str, row: int) -> list[str]:
    rows = re.findall(r"<tr[^>]*data-href[^>]*>(.*?)</tr>", html, flags=re.S)
    return re.findall(r"<td[^>]*>.*?</td>", rows[row], flags=re.S)


@pytest.mark.parametrize("path", ["/partial", "/rows", "/rows?raw=1"])
def test_a_span_cell_is_its_quote_then_its_page_linked(run_dir, path):
    html = _get(path)

    assert OPPOSE_CITE in html
    assert DISMISS_CITE in html


def test_a_null_span_cell_renders_empty(run_dir):
    cells = _cells_of_row(_get("/partial"), 1)

    assert cells[1:] == ["<td></td>", "<td></td>"]


def test_a_list_of_spans_renders_each_span(run_dir):
    quotes_cell = _cells_of_row(_get("/partial"), 0)[2]

    assert quotes_cell.count('class="span-cite"') == 2
    assert quotes_cell.index(OPPOSE_CITE) < quotes_cell.index(DISMISS_CITE)


def test_the_packet_stage_page_links_the_page_text_the_packet_writes(run_dir, tmp_path):
    packet = export_review_packet(PROJECT, RUN, tmp_path / "packets")

    page = (packet.root / "stages" / f"{STAGE}.html").read_text(encoding="utf-8")

    assert '<q title="oppose">oppose</q> <a href="../sources/ecf17/pages/14.txt">page 14</a>' in page
    assert "/project/" not in page


def test_a_long_quote_is_cut_short_and_its_title_holds_the_whole():
    quote = "The families of the crash victims oppose the motion to dismiss " * 3
    span = Span(source_id="ecf17", source_sha256="a" * 64,
                locator=PageCharRange(page=1, start=0, end=len(quote)), quote=quote)

    html = _render_span_cell(SpanCellText([span]), AppPanelLinks(PROJECT, RUN))

    assert f'title="{quote}"' in html
    assert f">{quote}<" not in html
    assert "...</q>" in html


def test_a_span_naming_no_page_links_its_file_in_the_app_and_nothing_in_a_packet():
    cell = Span(source_id="ledger", source_sha256="b" * 64,
                locator=CellAt(row=3, column="amount"), quote="12")

    served = _render_span_cell(SpanCellText([cell]), AppPanelLinks(PROJECT, RUN))
    packed = _render_span_cell(SpanCellText([cell]), PacketPanelLinks())

    assert f'<a href="/project/{PROJECT}/files/ledger">row 4, column amount</a>' in served
    assert "<a" not in packed
    assert "row 4, column amount" in packed


def test_a_changed_span_cell_in_a_diff_shows_the_value_it_replaced():
    html = _render_span_cell(SpanCellText([OPPOSE]), AppPanelLinks(PROJECT, RUN),
                             was=SpanCellText([DISMISS]))

    assert '<span class="diff-was" title="the input value this stage replaced">dismiss</span>' in html
    assert OPPOSE_CITE in html


def _render_span_cell(cell: SpanCellText, links: object, was: str = "") -> str:
    macro = templates.env.get_template("_data_cell.html").module.data_cell
    return str(macro(cell, "", was, False, links))
