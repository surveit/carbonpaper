"""The packet's lineage pages: which rows get one, and that nothing links to a page
that was never written."""
from __future__ import annotations

import csv
import json
import re

import pandas as pd

from conftest import as_inputs

from app.web.panel_links import PacketPanelLinks

_HREF = re.compile(r'(?:href|src)="([^"#]+?)"')
# The page builds its step and contributor links in the browser out of this blob,
# so they are NOT hrefs in the markup — a static link check cannot see them.
_VIEW = re.compile(r"^const V = (\{.*?\}), PROJECT = ", re.M | re.S)
_SCRIPT = re.compile(r"<script\b.*?</script>", re.S)
_COLUMNS = [{"name": "client", "type": "str", "nullable": False}]
_TOTAL = {"name": "total", "type": "int", "nullable": False}
_READS_SOURCE = {"input": "source", "columns": _COLUMNS}
_READS_TOTALS = {"input": "totals", "columns": _COLUMNS}
_TOTALS = pd.DataFrame({"client": ["a", "b", "c"], "total": [1, 1, 1]})
# Each type's own required config, and a signature consistent with it — the model
# refuses an aggregate whose signature does not read what it groups by.
_TYPE_EXTRAS = {
    "input_data": {
        "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": _COLUMNS},
    },
    "aggregate": {
        "aggregate": {"group_by": ["client"],
                      "aggregations": [{"output_column": "total", "formula": "count"}]},
        "signature": {"form": "replaces", "reads": [_READS_SOURCE],
                      "produces": [*_COLUMNS, _TOTAL]},
    },
    "report": {
        "report": {"format": "html_report"},
        "function": {"kind": "inline", "summary": "writes one file",
                     "code": "def transform(df, output_dir):\n    return []"},
        "signature": {"form": "replaces", "reads": [_READS_TOTALS], "produces": []},
    },
}


def _relative_links(html: str) -> list[str]:
    """Script BODIES are stripped: `${x}` inside JS is a template literal, not a link."""
    srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)
    return [
        href for href in srcs + _HREF.findall(_SCRIPT.sub("", html))
        if not href.startswith(("http://", "https://", "data:", "javascript:", "mailto:"))
    ]


def test_a_row_with_no_page_is_offered_no_link():
    links = PacketPanelLinks(traced=frozenset({("keep_ai_candidates", 4)}))
    assert links.row_trace("keep_ai_candidates", 4) == "../lineage/keep_ai_candidates/4.html"
    assert links.row_trace("keep_ai_candidates", 5) is None
    assert links.row_trace("other_stage", 4) is None


def test_a_stage_id_carrying_a_slash_cannot_widen_the_path():
    links = PacketPanelLinks(traced=None)
    assert links.row_trace("a/../../etc", 0) == "../lineage/a%2F..%2F..%2Fetc/0.html"


def test_the_seeds_are_the_rows_the_report_stage_linked(tmp_path):
    from app.runtime.citations import CitationProvider, save_citations
    from app.web.review_packet.lineage import _published_rows

    run_dir = tmp_path / "run"
    (run_dir / "outputs").mkdir(parents=True)
    provider = CitationProvider(project="p", run_id="r", tables=as_inputs({"totals": _TOTALS}))
    provider.cite_row("totals", 2)
    provider.cite_row("totals", 0)
    save_citations("p", "r", "report", provider)
    assert _published_rows(_run_view(3)) == [("totals", 2), ("totals", 0)]


def test_a_run_that_published_no_links_gets_no_pages(tmp_path):
    """Not a failure: a report stage that declares no provider promises nothing."""
    from app.web.review_packet.lineage import _published_rows

    (tmp_path / "outputs").mkdir(parents=True)
    assert _published_rows(_run_view(2)) == []


def _run_view(rows: int):
    from app.services.review_packet.views import RunView, StageView

    def stage(stage_id: str, stage_type: str, row_count: int, inputs: list[str]):
        return StageView(
            # The cohort writer reads the frame through this record, as the packet does.
            record={"stage_id": stage_id, "type": stage_type,
                    "output_path": f"outputs/{stage_id}.parquet",
                    "input_validation_report": [
                        {"phase": f"input:{i}", "ok": True} for i in inputs]},
            stage_id=stage_id, type=stage_type, status="ok",
            row_count=row_count, elapsed_ms=0, error=None, notes=[],
            output_path=f"outputs/{stage_id}.parquet", validations=[],
            data_file=f"data/{stage_id}.csv",
            definition_error=None,
        )

    return RunView(
        project="p", run_id="r", status="ok", started_at="t", finished_at="t",
        workflow_version="v", is_test_run=False, bust_cache=False, halted_at=[],
        dropped_columns={},
        stages=[
            stage("source", "input_data", 10, []),
            stage("totals", "aggregate", rows, ["source"]),
            stage("report", "report", 0, ["totals"]),
        ],
        inputs=[],
    )


def test_no_lineage_page_links_a_lineage_page_that_was_not_written(tmp_path):
    """The whole point of the surface: a dead link reads as checked until it is clicked."""
    packet = _export_demo_packet(tmp_path)
    pages = sorted(p for p in packet.glob("lineage/*/*.html") if ".from-" not in p.name)
    assert len(pages) == 4, "two published rows, each naming one contributor"
    followed = [
        (page, href)
        for page in pages
        for href in _view_links(page.read_text(encoding="utf-8"))
    ]
    assert len(followed) >= len(pages) + 2, "each aggregate row must link its contributor"
    broken = [(p.name, h) for p, h in followed if not (p.parent / h).resolve().exists()]
    assert not broken, f"{len(broken)} lineage link(s) have no page: {broken[:5]}"


def test_a_lineage_page_reaches_the_rest_of_the_packet_by_relative_path(tmp_path):
    """It is written two levels down, so every shared asset is reached with ../../."""
    page = (_export_demo_packet(tmp_path) / "lineage/totals/0.html").read_text(encoding="utf-8")
    outward = {h for h in _relative_links(page) if "lineage/" not in h}
    assert outward == {
        "../../assets/diagram_nodes.js", "../../assets/tooltip.js",
        "../../assets/cell-lineage.js", "../../assets/figure_text.js",
        "../../assets/palette.css",
        "../../assets/style.css", "../../assets/packet.css", "../../assets/favicon.svg",
        "../../index.html",
        # The Inputs pane names the input stage, and the packet writes that page.
        "../../stages/source.html",
    }


def test_the_packet_page_carries_the_three_tabs_with_no_column_bound(tmp_path):
    """The packet writes a page per ROW, so its headline names the row and no cell."""
    page = (_export_demo_packet(tmp_path) / "lineage/totals/0.html").read_text(encoding="utf-8")
    assert "<code>totals</code> row 1" in page  # ordinal 0, as a reader counts it
    assert '<span class="lin-value">' not in page
    for pane, label in [("rows", "Relevant rows"), ("values", "Relevant columns"),
                        ("inputs", "Input files")]:
        assert f'data-pane="{pane}">{label}' in page
    # A folder has no server, so the scope map is the one pane the packet cannot draw.
    assert "A folder has no server to ask" in page


def test_a_packet_whose_version_is_unreadable_says_so_where_the_paths_would_be(tmp_path):
    """The packet is exported with no stages here, so no stage's branches are known."""
    page = (_export_demo_packet(tmp_path) / "lineage/totals/0.html").read_text(encoding="utf-8")

    assert '<div class="lin-snav">' in page
    assert "the version this run pinned is unreadable" in page
    # Matched on markup: the page carries the pane's stylesheet either way.
    assert 'class="path-entry' not in page


def _demo_run(tmp_path):
    from app.runtime.lineage import RowLineage, RowParent
    from test_trace_helpers import write_run

    source = pd.DataFrame({"client": ["a", "b"], "spend": [1, 2]})
    run_dir = write_run(tmp_path, [
        {"id": "source", "type": "input_data", "parents": [], "df": source},
        # One aggregate row per client, each naming the source row it totalled —
        # a fan-in, so the page carries a contributor link to follow.
        {"id": "totals", "type": "aggregate", "parents": ["source"],
         "df": pd.DataFrame({"client": ["a", "b"], "total": [1, 1]}),
         "lineage": RowLineage([
             [RowParent("source", 0, kind="contribution")],
             [RowParent("source", 1, kind="contribution")],
         ])},
    ])
    return run_dir


def _export_demo_packet(tmp_path):
    _export_demo_lineage(tmp_path)
    return tmp_path / "packet"


def _export_demo_lineage(tmp_path):
    from app.runtime.citations import CitationProvider, save_citations
    from app.web.review_packet.lineage import write_packet_lineage

    run_dir = _demo_run(tmp_path)
    provider = CitationProvider(project="p", run_id="r", tables=as_inputs({"totals": _TOTALS}))
    for row in range(2):
        provider.cite_row("totals", row)
    save_citations("p", "r", "report", provider)

    root = tmp_path / "packet"
    root.mkdir()
    return write_packet_lineage(root, run_dir, _run_view(2), {}, _DEMO_MANIFEST)


# What the run recorded of the one file it read — the Inputs pane's whole source.
_DEMO_MANIFEST = {
    "input_bindings": {"source": {"files": [
        {"path": "/data/east.csv", "sha256": "e" * 64, "bytes": 791}], "source": "run"}},
    "parameters": {"limits": {"source": 50}},
    "stage_records": [{"stage_id": "source", "type": "input_data", "status": "ok",
                       "output_row_count": 2, "started_at": "2026-08-13T18:16:47"}],
}


def test_the_packet_page_names_the_file_the_run_read(tmp_path):
    """No server to ask, so the pane is rendered into the page rather than fetched."""
    page = (_export_demo_packet(tmp_path) / "lineage/totals/0.html").read_text(encoding="utf-8")

    assert "east.csv" in page
    assert "row cap <b>50</b>" in page
    # The packet has no /project/.../files route, so the name stands without a link.
    assert '<a href' not in page.split("east.csv")[0].rsplit("<h2>", 1)[-1]
    assert "read by " in page
    # The pane says whose inputs these are, so the reader never reads them as the row's.
    assert "These are the run's inputs" in page


def _view_links(html: str) -> list[str]:
    """Only the trace links: the stage pages are the packet's other half to write."""
    blob = _VIEW.search(html)
    assert blob, "the page must embed its view model — every link is built from it"
    view = json.loads(blob.group(1))
    return [
        node["links"]["trace"] for node in view["nodes"] if node["links"].get("trace")
    ] + [
        branch["links"]["trace"]
        for node in view["nodes"]
        for group in node.get("contributor_groups") or []
        for branch in group.get("named") or []
        if branch["links"].get("trace")
    ]


def test_a_wide_fan_in_is_reached_through_its_table_on_both_surfaces():
    """24 contributors is where the packet's trail used to die — now both link a table."""
    from app.web.panel_links import AppPanelLinks, PacketPanelLinks
    from app.web.trace_view import build_trace_view

    trace = _aggregate_trace(contributors=24)
    packet = build_trace_view(
        trace, {}, PacketPanelLinks(traced=None, owner=("totals", 0))
    )
    app = build_trace_view(trace, {}, AppPanelLinks("p", "r"))

    def group(view):
        return next(g for n in view["nodes"] for g in n["contributor_groups"])

    # Neither names 24 rows inline; each points at the cohort as a table.
    assert group(packet)["named"] == [] and group(app)["named"] == []
    assert group(packet)["rows_link"] == "../lineage/totals/0.from-spend_by_client.html"
    assert "ordinals=" in group(app)["rows_link"], "the app filters its rows view"


def _aggregate_trace(contributors: int):
    return {
        "run_id": "r", "start_stage": "totals", "start_row": 0,
        "steps": [{
            "stage_id": "totals", "stage_type": "aggregate", "row_ordinal": 0,
            "row": {"total": 1}, "columns_new": ["total"], "origin": "other",
            "branches": [
                {"stage_id": "spend_by_client", "row_ordinal": i,
                 "kind": "contribution", "columns": None}
                for i in range(contributors)
            ],
        }],
        "end": {"reached_origin": False, "at_stage": "totals",
                "message": "this row summarizes its inputs"},
    }


def test_the_index_links_land_on_a_section_the_directory_defines(tmp_path):
    """An anchor with no target scrolls nowhere and reads as a broken page."""
    packet = _export_demo_packet(tmp_path)
    directory = (packet / "lineage/index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'<section id="([^"]+)"', directory))
    assert ids == {"totals", "source"}, "every stage listed gets an anchor"


def test_a_fan_in_ships_the_rows_it_summarized_as_a_table_and_a_csv(tmp_path):
    """The reader checks the arithmetic, so the cohort has to be the cohort — not the stage."""
    packet = _export_demo_packet(tmp_path)
    csv_path = packet / "lineage/totals/0.from-source.csv"
    assert csv_path.exists(), "the fan-in's own rows, downloadable"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert [r["client"] for r in rows] == ["a"], "row 0 of totals was fed by source row 0 alone"

    page = (packet / "lineage/totals/0.from-source.html").read_text(encoding="utf-8")
    assert "0.from-source.csv" in page, "the table offers its own download"
    assert "../source/0.html" in page, "and each row opens its own lineage"


def test_a_moved_project_still_ships_the_inputs_its_run_read(tmp_path):
    """Bindings hold absolute paths, so relocating a project stales every one."""
    from app.services import workspace
    from app.services.review_packet.checksums import compute_sha256
    from app.services.review_packet.data import _locate_input
    from app.models.run_manifest import InputBinding

    # The new home IS the workspace root now — that is what "the project moved" means.
    workspace.set_projects_dir(tmp_path / "new_home")
    (tmp_path / "new_home" / "demo" / "data").mkdir(parents=True)
    moved = tmp_path / "new_home" / "demo" / "data" / "aliases.csv"
    moved.write_text("name,stands_for\na,A\n", encoding="utf-8")

    def binding(sha: str | None) -> InputBinding:
        return InputBinding(
            stage_id="input_aliases", path="/gone/demo/data/aliases.csv",
            filename="aliases.csv", sha256=sha, bytes=moved.stat().st_size, source="workflow",
        )

    assert _locate_input(binding(compute_sha256(moved)), "demo") == moved
    # A same-named file is not the same file, and an unhashed binding cannot say.
    assert _locate_input(binding("0" * 64), "demo") is None
    assert _locate_input(binding(None), "demo") is None


def test_the_packet_ships_no_syntax_highlighter():
    """124 KB of third-party code to colour tokens, in an artifact opened from a stranger."""
    from app.web.review_packet.pages import HLJS_STYLESHEET, read_app_cascade_order

    assert HLJS_STYLESHEET in read_app_cascade_order(), (
        "the app still links it; this test guards the packet's exclusion, "
        "and would otherwise pass by the sheet simply having been deleted"
    )


def test_a_walk_wider_than_the_budget_writes_its_nearest_rows_and_says_it_stopped(
    tmp_path, monkeypatch
):
    """One corpus-wide total reaches every input row, which used to write no page."""
    from app.web.review_packet import lineage as packet_lineage

    monkeypatch.setattr(packet_lineage, "PACKET_MAX_LINEAGE_PAGES", 2)
    report = _export_demo_lineage(tmp_path)
    root = tmp_path / "packet"

    assert report.stops_short
    assert len(report.traced) == 2
    pages = sorted(p.name for p in root.glob("lineage/*/*.html") if ".from-" not in p.name)
    assert pages == ["0.html", "1.html"], "the two rows the report stage linked"
    assert "The walk back stops here" in (
        root / "lineage" / "index.html").read_text(encoding="utf-8")


def test_a_walk_inside_the_budget_claims_no_rows_are_missing(tmp_path):
    assert not _export_demo_lineage(tmp_path).stops_short
