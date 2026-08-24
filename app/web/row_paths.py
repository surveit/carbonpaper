"""The paths pane: the service's answer, or the reason this run has none."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.branch_analysis import PathsBehindFigure, RowOrdinal
from app.models.schema import StageId
from app.runtime.branch_analysis import WorkflowRunBranches
from app.services.scope import find_contributing_rows, find_paths_behind


@dataclass(frozen=True)
class CitedFigure:
    """The row every path on the pane feeds, which no change of path moves."""

    stage_id: StageId
    row_ordinal: RowOrdinal


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
    return find_paths_behind(run_branches, covers.at_stage, covers.ordinals,
                             set(covers.regrained_at) | {figure.stage_id}, marked_row)
