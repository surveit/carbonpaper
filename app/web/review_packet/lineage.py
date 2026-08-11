"""The packet's row-lineage pages: one static page per traced row, rendered from
the same view model the served lineage page uses."""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from app.models import Stage
from app.runtime.trace import RunFrames, trace_row_from, trace_to_dict
from app.runtime.trace_links import read_issued_traces
from app.services.review_packet.views import RunView
from app.web.breadcrumbs import Crumb
from app.web.config import templates
from app.web.panel_links import PacketPanelLinks, packet_lineage_href
from app.web.review_packet.pages import STYLESHEETS
from app.web.trace_view import build_trace_view

_log = logging.getLogger(__name__)

LINEAGE_DIR = "lineage"

# A page per traced row. Reached only by a run whose terminal stages are very wide;
# past it the packet writes NO lineage at all rather than a partial set, because a
# row linking to a page that was never written is the one failure this whole surface
# exists to avoid — a dead link reads as "checked" until the reader clicks it.
PACKET_MAX_LINEAGE_PAGES = 20_000


class LineageReport(BaseModel):
    written: list[str]
    traced: set[tuple[str, int]]
    refused: str | None


def write_packet_lineage(
    root: Path, run_dir: Path, view: RunView, stages_by_id: dict[str, Stage]
) -> LineageReport:
    """Traces every row the run PUBLISHED a link to, and the rows feeding those."""
    frames = RunFrames(run_dir)
    closure = _find_closure(frames, run_dir)
    if len(closure) > PACKET_MAX_LINEAGE_PAGES:
        return LineageReport(written=[], traced=set(), refused=(
            f"{len(closure):,} rows feed the rows this run published, over the "
            f"{PACKET_MAX_LINEAGE_PAGES:,}-page limit — no lineage page was written, "
            "because a partial set would leave some rows looking unsourced"
        ))
    traced = frozenset(closure)
    written = [
        _write_page(root, frames, view, stages_by_id, stage_id, row, traced)
        for stage_id, row in sorted(closure)
    ]
    return LineageReport(written=written, traced=set(closure), refused=None)


def _find_closure(frames: RunFrames, run_dir: Path) -> set[tuple[str, int]]:
    """Every row reachable from a published link, following the branches a trace names."""
    pending = _published_rows(run_dir)
    seen: set[tuple[str, int]] = set()
    while pending:
        row = pending.pop()
        if row in seen:
            continue
        seen.add(row)
        if len(seen) > PACKET_MAX_LINEAGE_PAGES:
            return seen
        pending.extend(_branches_of(frames, *row))
    return seen


def _branches_of(frames: RunFrames, stage_id: str, row: int) -> list[tuple[str, int]]:
    trace = trace_to_dict(trace_row_from(frames, stage_id, row))
    return [
        (branch["stage_id"], branch["row_ordinal"])
        for step in trace["steps"] for branch in step["branches"]
    ]


def _write_page(
    root: Path, frames: RunFrames, view: RunView, stages_by_id: dict[str, Stage],
    stage_id: str, row: int, traced: frozenset[tuple[str, int]],
) -> str:
    trace = trace_to_dict(trace_row_from(frames, stage_id, row))
    relative = packet_lineage_href("", stage_id, row)
    html = templates.env.get_template("lineage.html").render(
        title=f"{stage_id} · row {row}",
        view=build_trace_view(
            trace, stages_by_id, PacketPanelLinks(to_root="../../", traced=traced)
        ),
        project=view.project,
        crumbs=[
            Crumb(label=view.project or "run", href="../../index.html"),
            Crumb(label=f"{stage_id} row {row}", is_code=True),
        ],
        mermaid="",
        offline=True,
        static_root="../../assets/",
        assets=[f"../../assets/{name}" for name in STYLESHEETS],
    )
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return relative


def _published_rows(run_dir: Path) -> list[tuple[str, int]]:
    """The rows a publish stage LINKED, recorded by the linker it was handed."""
    return [(t.stage_id, t.row_ordinal) for t in read_issued_traces(run_dir)]
