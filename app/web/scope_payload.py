"""What the scope page is handed: a figure's rows, their branches, and the cuts."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pyarrow as pa
from pydantic import BaseModel

from app.core.errors import RowOutOfRange, StageNotInRun
from app.core.frames import convert_cell_to_json_value, read_frame_table, read_native_scalar
from app.core.json_types import JsonScalar
from app.models.branch_analysis import (
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
    find_stages_beside_the_flow,
    find_stages_each_row_came_through,
    find_stages_on_route,
    measure_frame_scale,
)
from app.web.merge_alias import (
    AliasedMerge,
    alias_the_merges,
    find_branches_that_tell_rows_apart,
    name_the_groups,
)
from app.web.config import render_row_number
from app.web.diagrams import TYPE_GLYPH

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
    # The type's mark from the run page's tags, so a column says what kind of stage it is.
    glyph: str
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
    # These rows' own merges, never the figure's: they went into other groups.
    aliased_merges: dict[StageId, AliasedMerge]
    resolved_merges: list[StageId]
    nearest_merge: StageId | None


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
    # NOT the stages in its path: one it passed without branching is here, not there.
    came_through: list[list[StageId]]
    came_through_index: list[int]
    # Lookup tables this figure came through. Named, never drawn: see the legend.
    lookup_tables: list[StageId]
    branches: dict[BranchId, BranchOption]
    # Merge stages standing in for their groups. See docs/branch-analysis.md.
    aliased_merges: dict[StageId, AliasedMerge]
    resolved_merges: list[StageId]
    # Resolved whatever the reader asked for, so it is the one that cannot be folded.
    nearest_merge: StageId | None
    stages: list[DrawnStage]
    reach: list[BranchReach]
    scale: list[FrameScale]
    # Set on the map of a cut, whose rows are not the figure's and hold no figure bar.
    is_a_cut: bool = False


def build_scope_map(run_branches: WorkflowRunBranches, project_id: str, run_id: str,
                    outputs: Path, citation: StageOutputCellCitation,
                    expand: frozenset[StageId] = frozenset()) -> ScopeMap:
    if citation.stage_id not in run_branches.stages:
        raise StageNotInRun(f"no stage '{citation.stage_id}' in this run")
    cited_frame, cited = _read_the_cited_cell(outputs, citation)
    covers = find_contributing_rows(run_branches, cited.stage_id, cited.row_ordinal)
    frame = read_frame_table(outputs / f"{covers.at_stage}.parquet")
    cited_row = [(cited.stage_id, cited.row_ordinal)]
    # The nearest re-graining, plus any a reader expanded. docs/branch-analysis.md
    nearest = find_nearest_merge(run_branches, cited_row)
    resolved = ({nearest} if nearest else set()) | set(expand)
    route = find_stages_on_route(run_branches, cited_row)
    paths, _, index = group_rows_by_path(
        run_branches, covers.at_stage, covers.ordinals,
        find_branches_that_tell_rows_apart(run_branches, route, resolved))
    branches = _name_merge_groups(
        run_branches, outputs,
        _branches_on(run_branches, paths) | _removals_on(run_branches, route))
    aliased = alias_the_merges(
        run_branches, find_rows_reached_per_stage(run_branches, cited_row), resolved)
    came_through, came_through_index = _index_stages_come_through(run_branches, covers)
    shown = covers.ordinals[:CELL_ROWS]
    return ScopeMap(
        project_id=project_id, run_id=run_id, citation=cited, covers=covers,
        cited_row=_read_cited_row(cited_frame, cited.row_ordinal),
        sampled_from=len(covers.ordinals) if len(shown) < len(covers.ordinals) else None,
        rows=read_rows(frame, shown, index),
        columns=list(frame.column_names),
        branch_paths=paths, branch_path_index=index,
        came_through=came_through, came_through_index=came_through_index,
        lookup_tables=_name_the_lookups(
            run_branches, cited.stage_id, find_stages_on_route(run_branches, cited_row)),
        branches=branches,
        aliased_merges=aliased,
        resolved_merges=sorted(resolved),
        nearest_merge=nearest,
        # "show every stage" says every. docs/scope-map.md
        stages=_draw_stages(run_branches, route, cited.stage_id),
        reach=_count_reach(run_branches, branches, index, paths),
        scale=_measure_the_flow(run_branches, cited),
    )


def _index_stages_come_through(run_branches: WorkflowRunBranches, covers: RowSet
                             ) -> tuple[list[list[StageId]], list[int]]:
    per_row = find_stages_each_row_came_through(
        run_branches, covers.at_stage, covers.ordinals)
    distinct: dict[tuple[StageId, ...], int] = {}
    index = []
    for stages in per_row:
        seen = tuple(stages)
        distinct.setdefault(seen, len(distinct))
        index.append(distinct[seen])
    return [list(seen) for seen in distinct], index


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


def read_cut(run_branches: WorkflowRunBranches, outputs: Path, branch_id: BranchId,
             expand: frozenset[StageId] = frozenset()) -> CutRows | None:
    """The rows behind one branch: counts over all of them, cells over a sample."""
    at_stage, ordinals = find_rows_that_took(run_branches, branch_id)
    if not ordinals or at_stage not in run_branches.branch_paths:
        return None
    behind = [(at_stage, ordinal) for ordinal in ordinals]
    nearest = find_nearest_merge(run_branches, behind)
    resolved = ({nearest} if nearest else set()) | set(expand)
    paths, _, index = group_rows_by_path(
        run_branches, at_stage, ordinals,
        find_branches_that_tell_rows_apart(
            run_branches, find_stages_on_route(run_branches, behind), resolved))
    spread = Counter(index)
    shown = ordinals[:CUT_SAMPLE]
    frame = read_frame_table(outputs / f"{at_stage}.parquet")
    return CutRows(
        branch=branch_id, at_stage=at_stage, total=len(ordinals),
        columns=list(frame.column_names),
        branch_paths=paths,
        rows_per_branch_path=[spread[i] for i in range(len(paths))],
        rows=read_rows(frame, shown, index[:len(shown)]),
        stages=_draw_stages(run_branches, _stages_touched(run_branches, paths),
                            at_stage),
        aliased_merges=alias_the_merges(
            run_branches, find_rows_reached_per_stage(run_branches, behind), resolved),
        resolved_merges=sorted(resolved),
        nearest_merge=nearest,
    )


def build_scope_map_for_cut(scope: ScopeMap, cut: CutRows) -> ScopeMap:
    """The rows behind one cut as a map of their own: counts per path, no row named."""
    index = [path for path, rows in enumerate(cut.rows_per_branch_path)
             for _ in range(rows)]
    return scope.model_copy(update={
        "covers": RowSet(at_stage=cut.at_stage, ordinals=list(range(len(index)))),
        "branch_paths": cut.branch_paths, "branch_path_index": index,
        "rows": cut.rows, "columns": cut.columns, "stages": cut.stages,
        "reach": [], "scale": [], "sampled_from": cut.total,
        "aliased_merges": cut.aliased_merges, "resolved_merges": cut.resolved_merges,
        "nearest_merge": cut.nearest_merge,
        # A cut's rows arrive as counts per path, which name no frame each was in.
        "came_through": [], "came_through_index": [], "is_a_cut": True})


def find_cuts_to_offer(run_branches: WorkflowRunBranches, outputs: Path,
                       scope: ScopeMap, expand: frozenset[StageId] = frozenset()
                       ) -> dict[BranchId, CutRows]:
    """A branch that took rows out here. A merge's groups are asked for one at a time."""
    drawn = {branch_id for path in scope.branch_paths for branch_id in path}
    found: dict[BranchId, CutRows] = {}
    for branch_id, option in scope.branches.items():
        if branch_id in drawn or option.role is not BranchRole.removes:
            continue
        cut = read_cut(run_branches, outputs, branch_id, expand)
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
    return {branch_id: option
            for branch_id, option in run_branches.branch_options.items()
            if branch_id in held or (option.stage_id in touched
                                     and not _is_aliased(option, held))}


def _removals_on(run_branches: WorkflowRunBranches, route: set[StageId]
                 ) -> dict[BranchId, BranchOption]:
    """A cut below the drawn grain is on no drawn path, so `_branches_on` misses it."""
    return {branch_id: option
            for branch_id, option in run_branches.branch_options.items()
            if option.role is BranchRole.removes and option.stage_id in route}


def _name_merge_groups(run_branches: WorkflowRunBranches, outputs: Path,
                       branches: dict[BranchId, BranchOption]
                       ) -> dict[BranchId, BranchOption]:
    """A resolved merge's arms are groups, so each is named by its group_by values."""
    named: dict[StageId, dict[RowOrdinal, str]] = {}
    return {branch_id: _label_a_merge(option, named, run_branches, outputs)
            for branch_id, option in branches.items()}


def _is_aliased(option: BranchOption, held: set[BranchId]) -> bool:
    """A group no drawn row went into sits behind its stage's node, not on the page."""
    return option.reason is BranchReason.merge and option.id not in held


def _label_a_merge(option: BranchOption, named: dict[StageId, dict[RowOrdinal, str]],
                   run_branches: WorkflowRunBranches, outputs: Path) -> BranchOption:
    ordinal = option.merged_into_row_ordinal
    if ordinal is None:
        return option
    if option.stage_id not in named:
        named[option.stage_id] = name_the_groups(run_branches, outputs, option.stage_id)
    keys = named[option.stage_id].get(ordinal)
    return option.model_copy(update={
        "label": keys or f"merged into row {render_row_number(ordinal)}"})


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


def _measure_the_flow(run_branches: WorkflowRunBranches,
                      cited: StageOutputCellCitation) -> list[FrameScale]:
    """A lookup table's size is not the flow narrowing, and it is drawn no column here."""
    beside = find_stages_beside_the_flow(run_branches, cited.stage_id)
    return [step for step in measure_frame_scale(run_branches, cited)
            if step.stage not in beside]


def _name_the_lookups(run_branches: WorkflowRunBranches, from_stage: StageId,
                      on_route: set[StageId]) -> list[StageId]:
    """The lookup tables this figure read: named under the drawing, never drawn in it."""
    beside = find_stages_beside_the_flow(run_branches, from_stage)
    return [sid for sid in run_branches.ordered_stage_ids
            if sid in beside and sid in on_route]


def _draw_stages(run_branches: WorkflowRunBranches, touched: set[StageId],
                 from_stage: StageId) -> list[DrawnStage]:
    beside = find_stages_beside_the_flow(run_branches, from_stage)
    return [_draw_stage(run_branches, sid, position)
            for position, sid in enumerate(run_branches.ordered_stage_ids)
            if sid in touched and sid not in beside and sid in run_branches.stages]


def _draw_stage(run_branches: WorkflowRunBranches, sid: StageId,
                position: int) -> DrawnStage:
    stage = run_branches.stages[sid]
    authored = stage.stage
    return DrawnStage(
        id=sid, type=str(getattr(authored.type, "value", authored.type)),
        glyph=TYPE_GLYPH[authored.type],
        position=position, description=authored.description or "",
        code=read_stage_code(stage) or read_decision_source(stage))


def _plain(value: object) -> JsonScalar:
    return convert_cell_to_json_value(
        read_native_scalar(value) if hasattr(value, "as_py") else value)
