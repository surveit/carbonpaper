"""The packet's row-lineage pages: one static page per traced row, rendered from
the same view model the served lineage page uses."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.models import WorkflowStage
from app.core.frames import write_frame_file
from app.runtime.trace import RunFrames, trace_row_from, trace_to_dict
from app.runtime.trace_links import read_issued_traces
from app.services.review_packet.views import (
    LINEAGE_DIR,
    LineageReport,
    PublishedFigure,
    RunView,
    StageTraces,
)
from app.web.breadcrumbs import Crumb
from app.web.config import templates
from app.web.panel_links import (
    PacketPanelLinks,
    packet_contributors_href,
    packet_lineage_href,
)
from app.web.review_packet.pages import ASSETS_DIR, STYLESHEETS
from app.web.trace_view import build_trace_view

_log = logging.getLogger(__name__)

# A page per traced row. Reached only by a run whose terminal stages are very wide;
# past it the packet writes NO lineage at all rather than a partial set, because a
# row linking to a page that was never written is the one failure this whole surface
# exists to avoid — a dead link reads as "checked" until the reader clicks it.
PACKET_MAX_LINEAGE_PAGES = 20_000


def write_packet_lineage(
    root: Path, run_dir: Path, view: RunView, stages_by_id: dict[str, WorkflowStage]
) -> LineageReport:
    """Traces every row the run PUBLISHED a link to, and the rows feeding those."""
    frames = RunFrames(run_dir)
    published = set(_published_rows(run_dir))
    closure = _find_closure(frames, run_dir)
    if len(closure) > PACKET_MAX_LINEAGE_PAGES:
        return LineageReport(written=[], traced=set(), refused=(
            f"{len(closure):,} rows feed the rows this run published, over the "
            f"{PACKET_MAX_LINEAGE_PAGES:,}-page limit — no lineage page was written, "
            "because a partial set would leave some rows looking unsourced"
        ))
    traced = frozenset(closure)
    written = [
        path
        for stage_id, row in sorted(closure)
        for path in _write_page(root, frames, view, stages_by_id, stage_id, row, traced)
    ]
    stages = _group_by_stage(sorted(closure), published)
    figures = _named_figures(run_dir, closure)
    if written:
        # No directory where there is nothing to list: a run that published no
        # links promises no provenance, and an empty page implies otherwise.
        written.append(_write_directory(root, stages, len(closure)))
    return LineageReport(
        written=written, traced=set(closure), refused=None,
        stages=stages, figures=figures,
    )


def _named_figures(run_dir: Path, closure: set[tuple[str, int]]) -> list[PublishedFigure]:
    """Only targets the publish stage named; an unnamed one is a row, not a figure."""
    return [
        PublishedFigure(
            label=target.label,
            value=target.value,
            stage_id=target.stage_id,
            row_ordinal=target.row_ordinal,
            href=(
                packet_lineage_href("", target.stage_id, target.row_ordinal)
                if (target.stage_id, target.row_ordinal) in closure else None
            ),
        )
        for target in read_issued_traces(run_dir) if target.label
    ]


def _group_by_stage(
    traced: list[tuple[str, int]], published: set[tuple[str, int]]
) -> list[StageTraces]:
    by_stage: dict[str, list[int]] = {}
    for stage_id, row in traced:
        by_stage.setdefault(stage_id, []).append(row)
    return [
        StageTraces(
            stage_id=stage_id,
            rows=rows,
            published=sum(1 for r in rows if (stage_id, r) in published),
            hrefs=[packet_lineage_href("", stage_id, r) for r in rows],
        )
        for stage_id, rows in by_stage.items()
    ]


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
    root: Path, frames: RunFrames, view: RunView, stages_by_id: dict[str, WorkflowStage],
    stage_id: str, row: int, traced: frozenset[tuple[str, int]],
) -> list[str]:
    trace = trace_to_dict(trace_row_from(frames, stage_id, row))
    relative = packet_lineage_href("", stage_id, row)
    written = _write_contributor_tables(root, frames, view, trace, stage_id, row)
    html = templates.env.get_template("lineage.html").render(
        title=f"{stage_id} · row {row}",
        view=build_trace_view(
            trace, stages_by_id,
            PacketPanelLinks(to_root="../../", traced=traced, owner=(stage_id, row)),
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
    return [relative, *written]


def _published_rows(run_dir: Path) -> list[tuple[str, int]]:
    """The rows a publish stage LINKED, recorded by the linker it was handed."""
    return [(t.stage_id, t.row_ordinal) for t in read_issued_traces(run_dir)]


def _write_directory(root: Path, stages: list[StageTraces], total: int) -> str:
    relative = f"{LINEAGE_DIR}/index.html"
    html = templates.env.get_template("packet_lineage_index.html").render(
        stages=stages,
        total=total,
        assets=[f"../{ASSETS_DIR}/{name}" for name in STYLESHEETS],
        index_href="../index.html",
    )
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return relative


def _write_contributor_tables(
    root: Path, frames: RunFrames, view: RunView, trace: dict[str, Any],
    stage_id: str, row: int,
) -> list[str]:
    """One table per fan-in: exactly the rows that fed this one, as HTML and CSV."""
    return [
        path
        for source_id, ordinals in _contributions(trace).items()
        for path in _write_one_cohort(root, frames, view, stage_id, row, source_id, ordinals)
    ]


def _contributions(trace: dict[str, Any]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = {}
    for step in trace["steps"]:
        for branch in step["branches"]:
            groups.setdefault(branch["stage_id"], []).append(branch["row_ordinal"])
    return groups


def _write_one_cohort(
    root: Path, frames: RunFrames, view: RunView,
    stage_id: str, row: int, source_id: str, ordinals: list[int],
) -> list[str]:
    record = next((s.record for s in view.stages if s.stage_id == source_id), None)
    frame = frames.output(record) if record else None
    if frame is None:
        return []
    cohort = frame.iloc[[o for o in ordinals if 0 <= o < len(frame)]]
    csv_rel = packet_contributors_href("", stage_id, row, source_id, suffix="csv")
    html_rel = packet_contributors_href("", stage_id, row, source_id)
    (root / csv_rel).parent.mkdir(parents=True, exist_ok=True)
    write_frame_file(cohort, root / csv_rel)
    (root / html_rel).write_text(
        templates.env.get_template("packet_contributors.html").render(
            source_id=source_id, owner_stage=stage_id, owner_row=row,
            columns=[str(c) for c in cohort.columns],
            rows=[{str(k): _cell(v) for k, v in r.items()} for _, r in cohort.iterrows()],
            ordinals=list(cohort.index),
            csv_href=Path(csv_rel).name,
            owner_href=f"{row}.html",
            assets=[f"../../{ASSETS_DIR}/{name}" for name in STYLESHEETS],
            index_href="../../index.html",
        ),
        encoding="utf-8",
    )
    return [html_rel, csv_rel]


def _cell(value: Any) -> str:
    return "" if value is None else str(value)
