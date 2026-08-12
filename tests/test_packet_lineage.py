"""The packet's lineage pages: which rows get one, and that nothing links to a page
that was never written."""
from __future__ import annotations

import csv
import json
import re

import pandas as pd

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
    "publish": {
        "publish": {"format": "html_report"},
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


def test_the_seeds_are_the_rows_the_publish_stage_linked(tmp_path):
    from app.runtime.trace_links import RowTraceLinker, write_issued_traces
    from app.web.review_packet.lineage import _published_rows

    run_dir = tmp_path / "run"
    (run_dir / "outputs").mkdir(parents=True)
    linker = RowTraceLinker(project="p", run_id="r")
    linker.build_row_trace_url("totals", 2)
    linker.build_row_trace_url("totals", 0)
    write_issued_traces(run_dir, "report", linker)
    assert _published_rows(run_dir) == [("totals", 2), ("totals", 0)]


def test_a_run_that_published_no_links_gets_no_pages(tmp_path):
    """Not a failure: a publish stage that declares no `trace_links` promises nothing."""
    from app.web.review_packet.lineage import _published_rows

    (tmp_path / "outputs").mkdir(parents=True)
    assert _published_rows(tmp_path) == []


def _run_view(rows: int):
    from app.models.stage import parse_stage
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
            definition=parse_stage({
                "id": stage_id, "description": stage_id, "type": stage_type,
                "inputs": [{"id": i, "schema": {"columns": _COLUMNS}} for i in inputs],
                **_TYPE_EXTRAS[stage_type],
            }),
            definition_error=None,
        )

    return RunView(
        project="p", run_id="r", status="ok", started_at="t", finished_at="t",
        workflow_version="v", is_test_run=False, bust_cache=False, halted_at=[],
        dropped_columns={},
        stages=[
            stage("source", "input_data", 10, []),
            stage("totals", "aggregate", rows, ["source"]),
            stage("report", "publish", 0, ["totals"]),
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
        "../../assets/diagram_nodes.js", "../../assets/palette.css",
        "../../assets/style.css", "../../assets/packet.css", "../../index.html",
    }


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
    from app.runtime.trace_links import RowTraceLinker, write_issued_traces
    from app.web.review_packet.lineage import write_packet_lineage

    run_dir = _demo_run(tmp_path)
    linker = RowTraceLinker(project="p", run_id="r")
    for row in range(2):
        linker.build_row_trace_url("totals", row)
    write_issued_traces(run_dir, "report", linker)

    root = tmp_path / "packet"
    root.mkdir()
    write_packet_lineage(root, run_dir, _run_view(2), {})
    return root


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


def test_a_fan_in_wider_than_the_app_names_is_still_clickable_in_the_packet():
    """A 24-contributor aggregate is where the packet's trail used to die."""
    from app.web.panel_links import AppPanelLinks, PacketPanelLinks
    from app.web.trace_view import build_trace_view

    trace = _aggregate_trace(contributors=24)
    packet = build_trace_view(trace, {}, PacketPanelLinks(traced=None))
    app = build_trace_view(trace, {}, AppPanelLinks("p", "r"))

    def named(view):
        return [b["links"]["trace"] for n in view["nodes"]
                for g in n["contributor_groups"] for b in g["named"]]

    assert len(named(packet)) == 24, "the packet names every contributor it can open"
    assert named(app) == [], "the app still falls back to its filtered rows view"
    # And the wide-cohort fallback points at data a reader can actually read.
    assert [g["rows_link"] for n in packet["nodes"] for g in n["contributor_groups"]] == [
        "../data/spend_by_client.csv"
    ]


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
    ids = set(re.findall(r'<section class="lin-dir" id="([^"]+)"', directory))
    assert ids == {"totals", "source"}, "every stage listed gets an anchor"


def test_a_publish_input_row_with_no_page_is_offered_no_link(tmp_path):
    """The index lists every publish input, including ones the run never linked."""
    from app.web.review_packet.pages import _one_publish_input
    from app.web.panel_links import PacketPanelLinks

    run_dir = _demo_run(tmp_path)
    view = _run_view(2)
    traced = frozenset({("totals", 0)})
    built = _one_publish_input(
        run_dir, view, "totals", PacketPanelLinks(to_root="", traced=traced), traced
    )
    assert built.rows_total == 2
    assert built.trace_hrefs == ["lineage/totals/0.html", None], (
        "row 1 has no page, so it gets no link"
    )


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
