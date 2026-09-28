"""A span cell renders as its quote and the page it sits on, linked, in every table of a run."""
from __future__ import annotations

import copy
import json
import pickle
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.locators import CellAt, PageCharRange
from app.models.spans import Span
from app.models.stage import parse_stage
from app.web.config import templates
from app.web.diff_state import CellDiffState
from app.web.panel_links import AppPanelLinks, PacketPanelLinks
from app.web.review_packet import export_review_packet
from app.web.span_cells import SpanCellText
from app.web.stage_diff import build_stage_diff
from conftest import place_stage
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
# Row 1's nulls are what make pandas float a struct's ints, which a span must survive.
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


def test_the_lineage_row_view_hands_its_script_each_quote_and_page_link(run_dir):
    view = _read_lineage_view(_get("/row/0/trace/view"))

    columns = {column["name"]: column for column in view["nodes"][-1]["row_diff"]["columns"]}
    oppose = {"quote": "oppose", "label": "page 14",
              "href": f"/project/{PROJECT}/files/ecf17?page=14&start=120&end=126"}
    assert columns["quote"]["cites"] == [oppose]
    assert [cite["label"] for cite in columns["quotes"]["cites"]] == ["page 14", "page 2"]
    assert columns["quote"]["text"] == "oppose"
    assert columns["claim"]["cites"] == []


def test_a_packet_stage_page_links_no_page_text_the_packet_does_not_hold(run_dir, tmp_path):
    packet = export_review_packet(PROJECT, RUN, tmp_path / "packets")

    page = (packet.root / "stages" / f"{STAGE}.html").read_text(encoding="utf-8")

    assert '<q title="oppose">oppose</q> page 14' in page
    assert "sources/" not in page and "/project/" not in page


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


def test_a_span_cell_survives_a_deep_copy_and_a_pickle_with_its_spans():
    cell = SpanCellText([OPPOSE, DISMISS])

    for copied in (copy.deepcopy(cell), pickle.loads(pickle.dumps(cell))):
        assert copied == cell
        assert [cite.span for cite in copied.cites] == [OPPOSE, DISMISS]


def test_the_same_quote_moved_to_another_page_reads_as_changed_in_the_stage_diff(tmp_path):
    moved = Span(source_id="ecf17", source_sha256="a" * 64,
                 locator=PageCharRange(page=3, start=10, end=16), quote="oppose")
    (tmp_path / "outputs").mkdir()
    pq.write_table(pa.table({"quote": pa.array([OPPOSE.model_dump()] * 2)}),
                   tmp_path / "outputs" / "load.parquet")
    pq.write_table(pa.table({"quote": pa.array([moved.model_dump(), OPPOSE.model_dump()])}),
                   tmp_path / "outputs" / "move.parquet")
    stage = place_stage(parse_stage({
        "id": "move", "description": "Move a quote", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "function": {"kind": "inline", "code": "def transform(row):\n    return row\n"},
        "signature": {"form": "extends", "adds": []},
    }))

    diff = build_stage_diff(stage, tmp_path, "outputs/move.parquet",
                            {"load": "outputs/load.parquet"})

    assert diff is not None and diff.changed_cells_total == 1
    moved_cell, kept_cell = (row[0] for row in diff.rows)
    assert moved_cell.state is CellDiffState.changed
    assert [cite.label for cite in moved_cell.was.cites] == ["page 14"]
    assert [cite.label for cite in moved_cell.text.cites] == ["page 3"]
    assert kept_cell.state is CellDiffState.carried


def _read_lineage_view(html: str) -> dict:
    start = html.index("const V = ") + len("const V = ")
    view, _ = json.JSONDecoder().raw_decode(html, start)
    return view


def _render_span_cell(cell: SpanCellText, links: object, was: str = "") -> str:
    return templates.env.from_string(
        '{% from "_data_cell.html" import data_cell %}{{ data_cell(cell, "", was, false, links) }}'
    ).render(cell=cell, was=was, links=links)
