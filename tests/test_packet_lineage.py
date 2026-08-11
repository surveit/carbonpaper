"""The packet's lineage pages: which rows get one, and that nothing links to a page
that was never written."""
from __future__ import annotations

import json
import re

import pandas as pd
import pytest

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


@pytest.mark.parametrize("published_rows", [3, 1])
def test_publish_is_transparent_so_its_inputs_are_the_seeds(tmp_path, published_rows):
    from app.web.review_packet.lineage import _terminal_rows

    view = _run_view(published_rows)
    seeds = _terminal_rows(view)
    # publish carries no rows, so seeding off it directly would seed nothing at all.
    assert {stage_id for stage_id, _ in seeds} == {"totals"}
    assert sorted(row for _, row in seeds) == list(range(published_rows))


def _run_view(rows: int):
    from app.models.stage import parse_stage
    from app.services.review_packet.views import RunView, StageView

    def stage(stage_id: str, stage_type: str, row_count: int, inputs: list[str]):
        return StageView(
            record={}, stage_id=stage_id, type=stage_type, status="ok",
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
    pages = sorted(packet.glob("lineage/**/*.html"))
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


def _export_demo_packet(tmp_path):
    from app.runtime.lineage import RowLineage, RowParent
    from app.web.review_packet.lineage import write_packet_lineage
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
