"""What the scope page is handed: a figure's rows, their branches, and the cuts."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pyarrow as pa
from pydantic import BaseModel

from app.core.errors import RowOutOfRange, StageNotInRun
from app.core.frames import read_frame_table
from app.core.json_types import JsonScalar
from app.models.branch_analysis import (
    AliasedMerge,
    BranchId,
    BranchOption,
    BranchPath,
    BranchReason,
    BranchRole,
    FrameScale,
    RowOrdinal,
    RowSet,
)
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.runtime.branch_analysis import (
    WorkflowRunBranches,
    find_rows_that_took,
    group_rows_by_path,
)
from app.runtime.branch_analysis.stage_code import read_decision_source, read_stage_code
from app.services.scope import (
    find_contributing_rows,
    find_nearest_merge,
    find_rows_reached_per_stage,
    find_stages_on_route,
    measure_frame_scale,
)
from app.web.merge_alias import alias_the_merges
from app.web.config import render_row_number

# Cells for a wider set than this are sampled; the counts never are.
CELL_ROWS = 400
# A cut can be most of the corpus. Its counts are exact; these are the cells shown.
CUT_SAMPLE = 25
# Where every load branch is drawn, since no one stage read the whole run off disk.
SOURCE_COLUMN = "(source)"


class DrawnRow(BaseModel):
    """One row of a frame, positional against the map's `columns`, as tables are here."""

    ordinal: RowOrdinal
    # The same row as the table heads it. See `render_row_number`.
    number: str
    branch_path_index: int
    cells: list[JsonScalar]


class DrawnStage(BaseModel):
    """A column of the drawing: a stage that told these rows apart."""

    id: StageId
    type: str
    description: str
    # Index in the run's execution order: the drawing's left-to-right.
    position: int
    code: str = ""


class CitedRow(BaseModel):
    """The figure's own output row, shown when a reader clicks the figure itself."""

    ordinal: RowOrdinal
    number: str
    columns: list[str]
    cells: list[JsonScalar]


class BranchReach(BaseModel):
    """`taken` counts every row of the run; `here` only this figure's."""

    branch: BranchId
    taken: int
    here: int


class CutRows(BaseModel):
    """The rows behind one branch this figure's rows did not take."""

    branch: BranchId
    at_stage: StageId
    total: int
    columns: list[str]
    branch_paths: list[BranchPath]
    # Parallel to `branch_paths`: how many rows took each one.
    rows_per_branch_path: list[int]
    rows: list[DrawnRow]
    stages: list[DrawnStage]


class ScopeMap(BaseModel):
    """Which rows produced one cited figure, and what told them apart from the rest."""

    project_id: str
    run_id: str
    citation: StageOutputCellCitation
    covers: RowSet
    cited_row: CitedRow
    # Set when `rows` was sampled, so a reader never mistakes a sample for the whole.
    sampled_from: int | None = None
    rows: list[DrawnRow]
    columns: list[str]
    branch_paths: list[BranchPath]
    branch_path_index: list[int]
    branches: dict[BranchId, BranchOption]
    # Merge stages standing in for their groups. See docs/branch-analysis.md.
    aliased_merges: dict[StageId, AliasedMerge]
    resolved_merge: StageId | None
    stages: list[DrawnStage]
    reach: list[BranchReach]
    scale: list[FrameScale]


def build_scope_map(run_branches: WorkflowRunBranches, project_id: str, run_id: str,
                    outputs: Path, citation: StageOutputCellCitation) -> ScopeMap:
    if citation.stage_id not in run_branches.stages:
        raise StageNotInRun(f"no stage '{citation.stage_id}' in this run")
    cited_frame, cited = _read_the_cited_cell(outputs, citation)
    covers = find_contributing_rows(run_branches, cited.stage_id, cited.row_ordinal)
    frame = read_frame_table(outputs / f"{covers.at_stage}.parquet")
    cited_row = [(cited.stage_id, cited.row_ordinal)]
    # Only the nearest re-graining is resolved. docs/branch-analysis.md
    nearest = find_nearest_merge(run_branches, cited_row)
    paths, _, index = group_rows_by_path(
        run_branches, covers.at_stage, covers.ordinals,
        find_stages_on_route(run_branches, cited_row), nearest)
    branches = _branches_on(run_branches, paths)
    shown = covers.ordinals[:CELL_ROWS]
    return ScopeMap(
        project_id=project_id, run_id=run_id, citation=cited, covers=covers,
        cited_row=_read_cited_row(cited_frame, cited.row_ordinal),
        sampled_from=len(covers.ordinals) if len(shown) < len(covers.ordinals) else None,
        rows=read_rows(frame, shown, index),
        columns=list(frame.column_names),
        branch_paths=paths, branch_path_index=index,
        branches=branches,
        aliased_merges=alias_the_merges(
            run_branches, find_rows_reached_per_stage(run_branches, cited_row), nearest),
        resolved_merge=nearest,
        stages=_draw_stages(run_branches, _stages_touched(run_branches, paths)),
        reach=_count_reach(run_branches, branches, index, paths),
        scale=measure_frame_scale(run_branches, cited),
    )


def _read_the_cited_cell(outputs: Path, citation: StageOutputCellCitation
                         ) -> tuple[pa.Table, StageOutputCellCitation]:
    """The citation with the cell's real value on it, read out of the run's frame."""
    path = outputs / f"{citation.stage_id}.parquet"
    if not path.exists():
        raise StageNotInRun(f"no stage '{citation.stage_id}' in this run")
    frame = read_frame_table(path)
    if citation.column not in frame.column_names:
        raise StageNotInRun(
            f"stage '{citation.stage_id}' has no column '{citation.column}'")
    if citation.row_ordinal >= frame.num_rows:
        raise RowOutOfRange(f"stage '{citation.stage_id}' has {frame.num_rows} rows")
    return frame, citation.model_copy(update={
        "value": _plain(frame.column(citation.column)[citation.row_ordinal])})


def _read_cited_row(frame: pa.Table, ordinal: RowOrdinal) -> CitedRow:
    return CitedRow(
        ordinal=ordinal, number=render_row_number(ordinal),
        columns=list(frame.column_names),
        cells=[_plain(frame.column(name)[ordinal]) for name in frame.column_names])


def read_cut(run_branches: WorkflowRunBranches, outputs: Path,
             branch_id: BranchId) -> CutRows | None:
    """The rows behind one branch: counts over all of them, cells over a sample."""
    at_stage, ordinals = find_rows_that_took(run_branches, branch_id)
    if not ordinals or at_stage not in run_branches.branch_paths:
        return None
    behind = [(at_stage, ordinal) for ordinal in ordinals]
    paths, _, index = group_rows_by_path(
        run_branches, at_stage, ordinals,
        find_stages_on_route(run_branches, behind),
        find_nearest_merge(run_branches, behind))
    spread = Counter(index)
    shown = ordinals[:CUT_SAMPLE]
    frame = read_frame_table(outputs / f"{at_stage}.parquet")
    return CutRows(
        branch=branch_id, at_stage=at_stage, total=len(ordinals),
        columns=list(frame.column_names),
        branch_paths=paths,
        rows_per_branch_path=[spread[i] for i in range(len(paths))],
        rows=read_rows(frame, shown, index[:len(shown)]),
        stages=_draw_stages(run_branches, _stages_touched(run_branches, paths)),
    )


def find_cuts_to_offer(run_branches: WorkflowRunBranches, outputs: Path,
                       scope: ScopeMap) -> dict[BranchId, CutRows]:
    """A branch that took rows out here. A merge's groups are asked for one at a time."""
    drawn = {branch_id for path in scope.branch_paths for branch_id in path}
    found: dict[BranchId, CutRows] = {}
    for branch_id, option in scope.branches.items():
        if branch_id in drawn or option.role is not BranchRole.removes:
            continue
        cut = read_cut(run_branches, outputs, branch_id)
        if cut is not None:
            found[branch_id] = cut
    return found


def read_rows(frame: pa.Table, ordinals: list[RowOrdinal],
              branch_path_index: list[int]) -> list[DrawnRow]:
    cells = [frame.column(name).to_pylist() for name in frame.column_names]
    return [DrawnRow(ordinal=ordinal, number=render_row_number(ordinal),
                     branch_path_index=branch_path_index[position],
                     cells=[_plain(column[ordinal]) for column in cells])
            for position, ordinal in enumerate(ordinals)]


def _branches_on(run_branches: WorkflowRunBranches, paths: list[BranchPath]
                 ) -> dict[BranchId, BranchOption]:
    touched = _stages_touched(run_branches, paths)
    held = {branch_id for path in paths for branch_id in path}
    return {branch_id: _label_a_merge(option)
            for branch_id, option in run_branches.branch_options.items()
            if branch_id in held or (option.stage_id in touched
                                     and not _is_aliased(option, held))}


def _is_aliased(option: BranchOption, held: set[BranchId]) -> bool:
    """A group no drawn row went into sits behind its stage's node, not on the page."""
    return option.reason is BranchReason.merge and option.id not in held


def _label_a_merge(option: BranchOption) -> BranchOption:
    """What a merge says IS the row it merged into, and naming a row is a reader's layer's."""
    if option.merged_into_row_ordinal is None:
        return option
    return option.model_copy(update={
        "label": f"merged into row {render_row_number(option.merged_into_row_ordinal)}"})


def _stages_touched(run_branches: WorkflowRunBranches,
                    paths: list[BranchPath]) -> set[StageId]:
    return {run_branches.branch_options[branch_id].stage_id
            for path in paths for branch_id in path}


def _count_reach(run_branches: WorkflowRunBranches,
                 branches: dict[BranchId, BranchOption],
                 branch_path_index: list[int],
                 paths: list[BranchPath]) -> list[BranchReach]:
    here: Counter[BranchId] = Counter()
    for index in branch_path_index:
        here.update(paths[index])
    return [BranchReach(branch=branch_id, taken=taken, here=here[branch_id])
            for branch_id, taken in run_branches.row_count_per_branch_id.items()
            if branch_id in branches]


def _draw_stages(run_branches: WorkflowRunBranches,
                 touched: set[StageId]) -> list[DrawnStage]:
    return [_draw_stage(run_branches, sid, position)
            for position, sid in enumerate(run_branches.ordered_stage_ids)
            if sid in touched and sid in run_branches.stages]


def _draw_stage(run_branches: WorkflowRunBranches, sid: StageId,
                position: int) -> DrawnStage:
    stage = run_branches.stages[sid]
    authored = stage.stage
    return DrawnStage(
        id=sid, type=str(getattr(authored.type, "value", authored.type)),
        position=position, description=authored.description or "",
        code=read_stage_code(stage) or read_decision_source(stage))


def _plain(value: object) -> JsonScalar:
    plain = value.as_py() if hasattr(value, "as_py") else value
    if isinstance(plain, float) and plain != plain:
        return None
    if isinstance(plain, (str, int, float, bool)) or plain is None:
        return plain
    return str(plain)  # a list cell, so the table can show it
