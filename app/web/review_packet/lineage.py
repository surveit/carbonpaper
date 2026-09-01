"""The packet's row-lineage pages: one static page per traced row, rendered from
the same view model the served lineage page uses."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.web.figure_text import render_figure
from app.core.json_types import JsonDict
from app.models import WorkflowStage
from app.core.frames import write_frame_file, table_to_frame
from app.runtime.trace import RunFrames, trace_row_from, trace_to_dict
from app.runtime.citations import read_cited_row_keys, read_citations
from app.services.review_packet.views import (
    LINEAGE_DIR,
    LineageReport,
    PublishedFigure,
    RunView,
    StageTraces,
)
from app.web.breadcrumbs import Crumb
from app.web.config import render_row_number, templates
from app.web.panel_links import (
    PacketPanelLinks,
    packet_contributors_href,
    packet_lineage_href,
)
from app.web.review_packet.pages import ASSETS_DIR, FAVICON, STYLESHEETS
from app.core.errors import RowOutOfRange, StageNotInRun
from app.runtime.branch_analysis import WorkflowRunBranches, reconstruct_run_branches
from app.runtime.errors import MissingLineage
from app.web.row_paths import (
    CitedFigure,
    NoPathsToShow,
    PathsPane,
    find_paths_behind_figure,
)
from app.web.trace_inputs import InputCatalog, build_input_catalog, read_run_inputs
from app.web.trace_view import build_trace_view, read_walked_rows

_log = logging.getLogger(__name__)

# A page per traced row. Reached only by a run whose terminal stages are very wide;
# past it the packet writes NO lineage at all rather than a partial set, because a
# row linking to a page that was never written is the one failure this whole surface
# exists to avoid — a dead link reads as "checked" until the reader clicks it.
PACKET_MAX_LINEAGE_PAGES = 20_000


def write_packet_lineage(
    root: Path, run_dir: Path, view: RunView, stages_by_id: dict[str, WorkflowStage],
    manifest: JsonDict,
) -> LineageReport:
    """Traces every row the run PUBLISHED a link to, and the rows feeding those."""
    frames = RunFrames(run_dir)
    branches = _read_run_branches(run_dir, view, stages_by_id)
    # Read once for the whole packet: every page below scopes this same catalog.
    catalog = build_input_catalog(view.project, manifest)
    published = set(_published_rows(view))
    closure = _find_closure(frames, view)
    if len(closure) > PACKET_MAX_LINEAGE_PAGES:
        return LineageReport(written=[], traced=set(), refused=(
            f"{render_figure(len(closure))} rows feed the rows this run published, over the "
            f"{render_figure(PACKET_MAX_LINEAGE_PAGES)}-page limit — no lineage page was written, "
            "because a partial set would leave some rows looking unsourced"
        ))
    traced = frozenset(closure)
    written = [
        path
        for stage_id, row in sorted(closure)
        for path in _write_page(root, frames, branches, view, stages_by_id,
                                catalog, stage_id, row, traced)
    ]
    stages = _group_by_stage(sorted(closure), published)
    figures = _named_figures(view, closure)
    if written:
        # No directory where there is nothing to list: a run that published no
        # links promises no provenance, and an empty page implies otherwise.
        written.append(_write_directory(root, stages, len(closure)))
    return LineageReport(
        written=written, traced=set(closure), refused=None,
        stages=stages, figures=figures,
    )


def _named_figures(view: RunView, closure: set[tuple[str, int]]) -> list[PublishedFigure]:
    """Every value a report stage cited; a row it merely claimed carries no figure."""
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
        for target in read_citations(view.project, view.run_id)
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


def _find_closure(frames: RunFrames, view: RunView) -> set[tuple[str, int]]:
    """Every row reachable from a published link, following the branches a trace names."""
    pending = _published_rows(view)
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
    root: Path, frames: RunFrames, branches: WorkflowRunBranches | None, view: RunView,
    stages_by_id: dict[str, WorkflowStage], catalog: InputCatalog, stage_id: str,
    row: int, traced: frozenset[tuple[str, int]],
) -> list[str]:
    trace = trace_to_dict(trace_row_from(frames, stage_id, row))
    relative = packet_lineage_href("", stage_id, row)
    written = _write_contributor_tables(root, frames, view, trace, stage_id, row)
    links = PacketPanelLinks(to_root="../../", traced=traced, owner=(stage_id, row))
    trace_view = build_trace_view(trace, stages_by_id, links)
    html = templates.env.get_template("lineage.html").render(
        title=f"{stage_id} · row {render_row_number(row)}",
        pane=_read_paths_pane(branches, stage_id, row, trace_view),
        figure=CitedFigure(stage_id=stage_id, row_ordinal=row),
        links=links,
        view=trace_view,
        inputs=read_run_inputs(catalog, links),
        project=view.project,
        crumbs=[
            Crumb(label=view.project or "run", href="../../index.html"),
            Crumb(label=f"{stage_id} row {render_row_number(row)}", is_code=True),
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


def _published_rows(view: RunView) -> list[tuple[str, int]]:
    """The rows this run's report stages cited, in the order they said so."""
    return read_cited_row_keys(view.project, view.run_id)


def _write_directory(root: Path, stages: list[StageTraces], total: int) -> str:
    relative = f"{LINEAGE_DIR}/index.html"
    html = templates.env.get_template("packet_lineage_index.html").render(
        stages=stages,
        total=total,
        assets=[f"../{ASSETS_DIR}/{name}" for name in STYLESHEETS],
        icon=f"../{ASSETS_DIR}/{FAVICON}",
        static_root=f"../{ASSETS_DIR}/",
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
    table = frames.output(record) if record else None
    if table is None:
        return []
    # The packet renders cells as text, which is what the pandas side of the
    # frames seam is for.
    frame = table_to_frame(table)
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
            icon=f"../../{ASSETS_DIR}/{FAVICON}",
            static_root=f"../../{ASSETS_DIR}/",
            index_href="../../index.html",
        ),
        encoding="utf-8",
    )
    return [html_rel, csv_rel]


def _cell(value: Any) -> str:
    return "" if value is None else str(value)


def _read_run_branches(
    run_dir: Path, view: RunView, stages_by_id: dict[str, WorkflowStage]
) -> WorkflowRunBranches | None:
    """None where this run's lineage cannot be read; the pages then say so."""
    if not stages_by_id:
        return None
    order = [stage.stage_id for stage in view.stages]
    rows = {stage.stage_id: stage.row_count for stage in view.stages}
    try:
        return reconstruct_run_branches(run_dir, stages_by_id, order, rows)
    except MissingLineage:
        return None


def _read_paths_pane(
    branches: WorkflowRunBranches | None, stage_id: str, row: int,
    trace_view: dict[str, Any],
) -> PathsPane:
    if branches is None:
        return NoPathsToShow(
            reason="the version this run pinned is unreadable, so no stage's branches are known")
    figure = CitedFigure(stage_id=stage_id, row_ordinal=row)
    try:
        return find_paths_behind_figure(branches, figure, read_walked_rows(trace_view))
    except (MissingLineage, StageNotInRun, RowOutOfRange) as no_paths:
        return NoPathsToShow(reason=str(no_paths))
