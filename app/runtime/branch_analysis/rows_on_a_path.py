"""Which rows took which whole path, beside rows_behind_a_branch's single-branch answer."""

from __future__ import annotations

from typing import NamedTuple

from app.models.branch_analysis import BranchPath, RowOrdinal
from app.models.schema import StageId
from app.runtime.branch_analysis.run_branches import WorkflowRunBranches


class PathsTaken(NamedTuple):
    paths: list[BranchPath]
    # The rows on each path, in the order they were asked about.
    ordinals: list[list[RowOrdinal]]
    path_of_row: list[int]  # one small int per row asked about, into `paths`


def group_rows_by_path(run_branches: WorkflowRunBranches, at_stage: StageId,
                       ordinals: list[RowOrdinal], on_route: set[StageId]
                       ) -> PathsTaken:
    paths: list[BranchPath] = []
    seen: dict[BranchPath, int] = {}
    path_of_row = []
    took: list[list[RowOrdinal]] = []
    for ordinal in ordinals:
        path = _keep_branches_on_route(
            run_branches, run_branches.branch_paths[at_stage][ordinal], on_route)
        if path not in seen:
            seen[path] = len(paths)
            paths.append(path)
            took.append([])
        path_of_row.append(seen[path])
        took[seen[path]].append(ordinal)
    return PathsTaken(paths=paths, ordinals=took, path_of_row=path_of_row)


def _keep_branches_on_route(run_branches: WorkflowRunBranches, path: BranchPath,
                            on_route: set[StageId]) -> BranchPath:
    """A decision taken at a stage these rows never came through tells them nothing."""
    options = run_branches.branch_options
    return tuple(branch_id for branch_id in path
                 if options[branch_id].stage_id in on_route)
