"""The paths pane: the distinct routes the rows behind one figure took."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pydantic import BaseModel

from app.models.branch_analysis import (
    BranchId,
    BranchOption,
    BranchPath,
    BranchReason,
    RowOrdinal,
    RowRef,
)
from app.models.schema import StageId
from app.runtime.branch_analysis import WorkflowRunBranches, group_rows_by_path
from app.runtime.errors import MissingLineage
from app.services.scope import (
    find_contributing_rows,
    find_sample_choices_behind,
    find_nearest_merge,
    find_stages_each_stage_feeds,
    find_stages_on_route,
)
from app.web.merge_alias import find_branches_that_tell_rows_apart


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
    # The row to sample at each fan-in, walking the figure to `example_ordinal`.
    sample_choices: list[RowRef] = []
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
    walked: Mapping[StageId, RowOrdinal],
) -> PathsBehindFigure:
    """`walked` is the row the page's own trace stood on at each stage it stepped through."""
    cited = (figure.stage_id, figure.row_ordinal)
    covers = find_contributing_rows(run_branches, *cited)
    _refuse_a_frame_with_no_paths(run_branches, covers.at_stage, covers.ordinals)
    # One re-graining is resolved, as on the scope map. docs/branch-analysis.md
    taken = group_rows_by_path(
        run_branches, covers.at_stage, covers.ordinals,
        find_branches_that_tell_rows_apart(
            run_branches, find_stages_on_route(run_branches, [cited]),
            _resolve_the_nearest(run_branches, cited)))
    choices = find_sample_choices_behind(run_branches, *cited)
    left_out = (_find_branches_on_every_path(taken.paths)
                | _find_loads_a_branch_below_them_restates(run_branches, taken.paths))
    marked_row = walked.get(covers.at_stage)
    return PathsBehindFigure(
        at_stage=covers.at_stage,
        paths=[_read_one_path(run_branches, path, on_it, left_out, choices, marked_row)
               for path, on_it in zip(taken.paths, taken.ordinals)],
    )


def _resolve_the_nearest(run_branches: WorkflowRunBranches,
                         cited: RowRef) -> set[StageId]:
    nearest = find_nearest_merge(run_branches, [cited])
    return {nearest} if nearest else set()


def _read_one_path(run_branches: WorkflowRunBranches, path: BranchPath,
                   ordinals: list[RowOrdinal], left_out: frozenset[BranchId],
                   choices: Mapping[RowOrdinal, tuple[RowRef, ...]],
                   marked_row: RowOrdinal | None) -> PathBehindFigure:
    options = [run_branches.branch_options[branch_id] for branch_id in path]
    return PathBehindFigure(
        rows=len(ordinals),
        tells_it_apart=[o for o in options if o.id not in left_out],
        whole_path=options,
        example_ordinal=ordinals[0],
        sample_choices=list(choices.get(ordinals[0], ())),
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


# A join or union under an input is WHY a row reached it, so it already says what
# that input's load branch says. Correlation is not enough: two branches can split
# the rows alike and still be different facts. docs/branch-analysis.md
_SAYS_WHY_A_ROW_REACHED_AN_INPUT = {BranchReason.join, BranchReason.union}


def _find_loads_a_branch_below_them_restates(
    run_branches: WorkflowRunBranches, paths: list[BranchPath],
) -> frozenset[BranchId]:
    if len(paths) < 2:
        return frozenset()
    feeds = find_stages_each_stage_feeds(run_branches)
    carried = _find_paths_carrying_each_branch(paths)
    return frozenset(
        branch_id for branch_id, option in run_branches.branch_options.items()
        if option.reason is BranchReason.load and branch_id in carried
        and carried[branch_id] <= _find_paths_reaching_that_input(
            run_branches, carried, feeds[option.stage_id]))


def _find_paths_reaching_that_input(run_branches: WorkflowRunBranches,
                                    carried: dict[BranchId, frozenset[int]],
                                    fed_stage_ids: set[StageId]) -> frozenset[int]:
    below = [carried[branch_id] for branch_id, option
             in run_branches.branch_options.items()
             if option.stage_id in fed_stage_ids and branch_id in carried
             and option.reason in _SAYS_WHY_A_ROW_REACHED_AN_INPUT]
    return frozenset[int]().union(*below) if below else frozenset()


def _find_paths_carrying_each_branch(
    paths: list[BranchPath],
) -> dict[BranchId, frozenset[int]]:
    carrying: dict[BranchId, set[int]] = {}
    for position, path in enumerate(paths):
        for branch_id in path:
            carrying.setdefault(branch_id, set()).add(position)
    return {branch_id: frozenset(positions)
            for branch_id, positions in carrying.items()}
