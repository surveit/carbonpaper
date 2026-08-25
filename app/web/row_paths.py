"""The paths pane: the distinct routes the rows behind one figure took.

Shaped here, beside app.web.scope_payload's own view models, because what a path
shows depends on the paths it is shown beside.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from app.models.branch_analysis import (
    BranchId,
    BranchOption,
    BranchPath,
    RowOrdinal,
)
from app.models.schema import StageId
from app.runtime.branch_analysis import WorkflowRunBranches, group_rows_by_path
from app.runtime.errors import MissingLineage
from app.services.scope import find_contributing_rows


@dataclass(frozen=True)
class CitedFigure:
    """The row every path on the pane feeds, which no change of path moves."""

    stage_id: StageId
    row_ordinal: RowOrdinal


class PathBehindFigure(BaseModel):
    rows: int
    tells_it_apart: list[BranchOption]  # held here, not on the paths beside it
    whole_path: list[BranchOption]
    example_ordinal: RowOrdinal
    holds_the_marked_row: bool = False


class PathsBehindFigure(BaseModel):
    at_stage: StageId
    paths: list[PathBehindFigure]

    @property
    def rows(self) -> int:
        return sum(path.rows for path in self.paths)


@dataclass(frozen=True)
class NoPathsToShow:
    """Why this run cannot say which paths its rows took."""

    reason: str


PathsPane = PathsBehindFigure | NoPathsToShow


def find_paths_behind_figure(
    run_branches: WorkflowRunBranches, figure: CitedFigure,
    marked_row: RowOrdinal | None = None,
) -> PathsBehindFigure:
    covers = find_contributing_rows(run_branches, figure.stage_id, figure.row_ordinal)
    _refuse_a_frame_with_no_paths(run_branches, covers.at_stage, covers.ordinals)
    taken = group_rows_by_path(run_branches, covers.at_stage, covers.ordinals,
                               set(covers.regrained_at) | {figure.stage_id})
    shared = _find_branches_on_every_path(taken.paths)
    return PathsBehindFigure(
        at_stage=covers.at_stage,
        paths=[_read_one_path(run_branches, path, on_it, shared, marked_row)
               for path, on_it in zip(taken.paths, taken.ordinals)],
    )


def _read_one_path(run_branches: WorkflowRunBranches, path: BranchPath,
                   ordinals: list[RowOrdinal], shared: frozenset[BranchId],
                   marked_row: RowOrdinal | None) -> PathBehindFigure:
    options = [run_branches.branch_options[branch_id] for branch_id in path]
    return PathBehindFigure(
        rows=len(ordinals),
        tells_it_apart=[o for o in options if o.id not in shared],
        whole_path=options,
        example_ordinal=ordinals[0],
        holds_the_marked_row=marked_row in ordinals,
    )


def _refuse_a_frame_with_no_paths(run_branches: WorkflowRunBranches, at_stage: StageId,
                                  ordinals: list[RowOrdinal]) -> None:
    """A stage the reconstruction never sized holds no path for any of its rows."""
    held = len(run_branches.branch_paths.get(at_stage) or [])
    if ordinals and held <= max(ordinals):
        raise MissingLineage(
            f"this run recorded paths for {held} rows of {at_stage}, "
            f"not the {max(ordinals) + 1} the figure reaches"
        )



def _find_branches_on_every_path(paths: list[BranchPath]) -> frozenset[BranchId]:
    """One path tells itself apart from nothing, so nothing of it reads as shared."""
    if len(paths) < 2:
        return frozenset()
    return frozenset.intersection(*(frozenset(path) for path in paths))
