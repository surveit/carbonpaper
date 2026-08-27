"""The ⌘K bar's index: every place it can send you, in the order it offers them.

Rank IS the list order — what you are inside comes before what you are not — so the
browser only filters. No row is a new destination: each is a link some page draws.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel

from app.services.loader import list_parsed_stages
from app.services.project import ProjectListing, list_project_listings, project_exists
from app.web.loading import load_stages_or_empty
from app.web.project_view import NavBlock, NavItem, build_nav
from app.web.run_index import RunIndexRow, build_run_index_rows, describe_run_outcome
from app.web.stage_strip import describe_stage_status


class CmdkPaletteKind(enum.StrEnum):
    SECTION = "section"
    STAGE = "stage"
    RUN = "run"
    PROJECT = "project"


class CmdkPaletteRow(BaseModel):
    """`meta` is matched on as well as shown, so a stage is findable by its project."""

    kind: CmdkPaletteKind
    label: str
    href: str
    meta: str = ""
    is_code: bool = False


class CmdkPaletteIndex(BaseModel):
    rows: list[CmdkPaletteRow]


def build_cmdk_palette_index(current_project: str, current_run: str = "") -> CmdkPaletteIndex:
    here = current_project if project_exists(current_project) else ""
    others = [item for item in list_project_listings() if item.id != here]
    return CmdkPaletteIndex(rows=[
        *_build_rows_inside(here, current_run),
        *[_build_project_row(item) for item in others],
        *[row for item in others for row in _build_stage_rows(item.id, here)],
        *[row for item in others for row in _build_run_rows(item.id, here)],
    ])


def _build_rows_inside(here: str, current_run: str) -> list[CmdkPaletteRow]:
    if not here:  # unknown, absent, or escaping the workspace — project_exists settled it
        return []
    runs = build_run_index_rows(here)
    open_run = next((run for run in runs if run.run_id == current_run), None)
    return [
        *_build_section_rows(here),
        # A stage means a different page depending on where it is read from: the
        # workflow defines it, a run is one execution of it. Reading a run, the id
        # the reader means is that run's, so the rows are its own and go nowhere else.
        *(_build_run_stage_rows(here, open_run) if open_run
          else _build_stage_rows(here, here)),
        *[_build_run_row(here, run, here) for run in runs],
    ]


def _build_section_rows(project_id: str) -> list[CmdkPaletteRow]:
    return [
        CmdkPaletteRow(kind=CmdkPaletteKind.SECTION, label=item.label, href=item.href)
        for item in _find_nav_pages(build_nav(project_id))
    ]


def _build_stage_rows(project_id: str, here: str) -> list[CmdkPaletteRow]:
    # Parsed stages only: one that does not parse has no id to offer.
    return [
        CmdkPaletteRow(
            kind=CmdkPaletteKind.STAGE,
            label=stage.id,
            href=f"/project/{project_id}/workflow#{stage.id}",
            meta=_describe_row(project_id, here, stage.description or ""),
            is_code=True,
        )
        for stage in list_parsed_stages(load_stages_or_empty(project_id).entries)
    ]


def _build_run_stage_rows(project_id: str, run: RunIndexRow) -> list[CmdkPaletteRow]:
    # The run's OWN squares, so the bar cannot name a stage this run never pinned.
    return [
        CmdkPaletteRow(
            kind=CmdkPaletteKind.STAGE,
            label=square.stage_id,
            href=f"/project/{project_id}/runs/{run.run_id}#{square.stage_id}",
            meta=describe_stage_status(square.status, run.status),
            is_code=True,
        )
        for square in (run.strip.squares if run.strip else [])
    ]


def _build_run_rows(project_id: str, here: str) -> list[CmdkPaletteRow]:
    return [_build_run_row(project_id, run, here) for run in build_run_index_rows(project_id)]


def _build_run_row(project_id: str, run: RunIndexRow, here: str) -> CmdkPaletteRow:
    return CmdkPaletteRow(
        kind=CmdkPaletteKind.RUN,
        label=run.run_id,
        href=f"/project/{project_id}/runs/{run.run_id}",
        meta=_describe_row(project_id, here, _describe_run(run)),
        is_code=True,
    )


def _build_project_row(listing: ProjectListing) -> CmdkPaletteRow:
    return CmdkPaletteRow(
        kind=CmdkPaletteKind.PROJECT, label=listing.name, href=f"/project/{listing.id}",
        meta=listing.id if listing.name != listing.id else "",
    )


def _describe_run(run: RunIndexRow) -> str:
    outcome = run.outcome or describe_run_outcome(run.status)
    return f"{outcome} · test run" if run.is_test_run else outcome


def _describe_row(project_id: str, here: str, detail: str) -> str:
    # Named only outside the project being read: inside it, the word is on every row.
    if project_id == here:
        return detail
    return f"{project_id} · {detail}" if detail else project_id


def _find_nav_pages(nav: list[NavBlock]) -> list[NavItem]:
    # A group heading opens no page, so it is nowhere the palette can jump to.
    return [item for block in nav for item in (block, *block.children)
            if isinstance(item, NavItem)]
