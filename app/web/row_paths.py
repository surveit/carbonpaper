"""The distinct paths rows took to reach one cited figure. docs/branch-analysis.md"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.branch_source import find_branches
from app.models.branch_analysis import (
    BranchOption,
    BranchPath,
    BranchReason,
    RowOrdinal,
    RowSet,
)
from app.models.schema import StageId
from app.runtime.branch_analysis import WorkflowRunBranches
from app.runtime.errors import MissingLineage
from app.runtime.branch_analysis.stage_code import read_stage_code, records_branch_arms
from app.services.scope import find_contributing_rows
from app.web.panel_links import PanelLinks
from app.web.scope_payload import index_paths


@dataclass(frozen=True)
class CitedFigure:
    """The row every path on the pane feeds, which no change of path moves."""

    stage_id: StageId
    row_ordinal: RowOrdinal


@dataclass(frozen=True)
class PathBehindFigure:
    # The group's size. Not shown: see the note in _row_paths.html.
    rows: int
    # The branches this path holds and at least one other path does not.
    tells_it_apart: list[BranchOption]
    whole_path: list[BranchOption]
    example_ordinal: RowOrdinal
    href: str | None
    carries_the_shown_row: bool


@dataclass(frozen=True)
class PathsBehindFigure:
    figure: CitedFigure
    at_stage: StageId
    rows: int
    paths: list[PathBehindFigure]
    # Stages whose code has arms this run did not record, so no path can name them.
    arms_not_recorded_at: list[StageId]


@dataclass(frozen=True)
class NoPathsToShow:
    """Why this run cannot say which paths its rows took."""

    reason: str


PathsPane = PathsBehindFigure | NoPathsToShow


def find_paths_behind(
    run_branches: WorkflowRunBranches, links: PanelLinks,
    figure: CitedFigure, shown_stage: StageId, shown_row: RowOrdinal,
) -> PathsBehindFigure:
    covers = find_contributing_rows(run_branches, figure.stage_id, figure.row_ordinal)
    _refuse_a_frame_with_no_paths(run_branches, covers)
    on_route = set(covers.regrained_at) | {figure.stage_id}
    paths, index = index_paths(run_branches, covers.at_stage, covers.ordinals, on_route)
    reader = _PathReader(
        run_branches=run_branches, links=links, at_stage=covers.at_stage, figure=figure,
        shown_stage=shown_stage, shown_row=shown_row,
        shared=_find_branches_on_every_path(paths),
    )
    took = _gather_ordinals_per_path(covers.ordinals, index, len(paths))
    return PathsBehindFigure(
        figure=figure,
        at_stage=covers.at_stage,
        rows=len(covers.ordinals),
        paths=[reader.describe(path, ordinals) for path, ordinals in zip(paths, took)],
        arms_not_recorded_at=find_stages_whose_arms_went_unrecorded(run_branches),
    )


def _refuse_a_frame_with_no_paths(
    run_branches: WorkflowRunBranches, covers: RowSet
) -> None:
    """A stage the reconstruction never sized holds no path for any of its rows."""
    held = len(run_branches.branch_paths.get(covers.at_stage) or [])
    if covers.ordinals and held <= max(covers.ordinals):
        raise MissingLineage(
            f"this run recorded paths for {held} rows of {covers.at_stage}, "
            f"not the {max(covers.ordinals) + 1} the figure reaches"
        )


def find_stages_whose_arms_went_unrecorded(
    run_branches: WorkflowRunBranches,
) -> list[StageId]:
    """A stage served wholly from cache never ran, so its arms were never written."""
    recorded = {option.stage_id for option in run_branches.branch_options.values()
                if option.reason is BranchReason.code}
    return [sid for sid in run_branches.ordered_stage_ids
            if sid not in recorded
            and records_branch_arms(stage := run_branches.stages.get(sid))
            and find_branches(read_stage_code(stage))]


@dataclass(frozen=True)
class _PathReader:
    run_branches: WorkflowRunBranches
    links: PanelLinks
    at_stage: StageId
    figure: CitedFigure
    shown_stage: StageId
    shown_row: RowOrdinal
    shared: frozenset[str]

    def describe(
        self, path: BranchPath, ordinals: list[RowOrdinal]
    ) -> PathBehindFigure:
        options = [self.run_branches.branch_options[branch_id] for branch_id in path]
        return PathBehindFigure(
            rows=len(ordinals),
            tells_it_apart=[o for o in options if o.id not in self.shared],
            whole_path=options,
            example_ordinal=ordinals[0],
            href=self.links.build_row_trace_for_figure(
                self.at_stage, ordinals[0], self.figure.stage_id,
                self.figure.row_ordinal),
            carries_the_shown_row=(
                self.shown_stage == self.at_stage and self.shown_row in ordinals),
        )


def _gather_ordinals_per_path(
    ordinals: list[RowOrdinal], index: list[int], paths: int
) -> list[list[RowOrdinal]]:
    took: list[list[RowOrdinal]] = [[] for _ in range(paths)]
    for at, which in enumerate(index):
        took[which].append(ordinals[at])
    return took


def _find_branches_on_every_path(paths: list[BranchPath]) -> frozenset[str]:
    """One path tells itself apart from nothing, so nothing of it reads as shared."""
    if len(paths) < 2:
        return frozenset()
    return frozenset.intersection(*(frozenset(path) for path in paths))
