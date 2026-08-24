"""Build one `ScopeMap`: which rows produced a figure, and what cut the rest.

See docs/scope-map.md.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pyarrow as pa

from app.core.errors import UnresolvableFigure
from app.core.frames import read_frame_table
from app.models.claims import StageOutputCellCitation
from app.models.scope_map import (
    BranchId,
    BranchOrigin,
    BranchPath,
    BranchReach,
    BranchRole,
    ContributingRow,
    ContributingRowSet,
    CutRows,
    FrameScale,
    RowOrdinal,
    ScalarCell,
    ScopeMap,
    StageId,
)
from app.models.stages.aggregate import AggFormula
from app.runtime.lineage import EdgeKind
from app.runtime.scope import (
    SOURCE_STAGE,
    RunBranches,
    find_reference_inputs,
)

CONTRIBUTION = EdgeKind.contribution.value
# The arm whose rows are in the stage's INPUT frame, never its output.
DROPPED_ARM = "dropped"
# Cells for a wider set than this are sampled; the counts never are.
CELL_ROWS = 400


def build_scope_map(run: RunBranches, project_id: str, run_id: str, outputs: Path,
                    citation: StageOutputCellCitation) -> ScopeMap:
    covers = find_contributing_rows(run, citation)
    frame = read_frame_table(outputs / f"{covers.at_stage}.parquet")
    on_route = set(covers.stages_traced_through) | {citation.stage_id}
    paths, path_index = index_paths(run, covers.at_stage, covers.ordinals, on_route)
    shown = covers.ordinals[:CELL_ROWS]
    covers.sampled_from = len(covers.ordinals) if len(shown) < len(covers.ordinals) else None
    value_column = _read_value_column(run, citation) if covers.adds_up else None
    return ScopeMap(
        project_id=project_id, run=run_id, citation=citation,
        formula=_read_formula(run, citation), value_column=value_column,
        covers=covers,
        rows=read_rows(frame, shown, path_index, value_column),
        columns=list(frame.column_names),
        paths=paths, path_index=path_index,
        branches=_branches_on(run, paths),
        stages=[entry for entry in run.read_branching_stages()
                if entry.id in _stages_touched(run, paths)],
        reach=_count_reach(run, paths, path_index),
        scale=measure_frame_scale(run, citation),
    )


def find_contributing_rows(run: RunBranches, citation: StageOutputCellCitation
                           ) -> ContributingRowSet:
    """Replace a group row by the rows that fed it, until none is a group."""
    reached, at_stage, through = _expand(run, citation.stage_id, citation.row_ordinal)
    return ContributingRowSet(
        at_stage=at_stage, ordinals=sorted(set(reached)),
        stages_traced_through=through,
        adds_up=_totals_by_adding(run, citation, through),
    )


def measure_frame_scale(run: RunBranches, citation: StageOutputCellCitation
                        ) -> list[FrameScale]:
    """Per stage: rows in the frame, and how many of them the figure descends from."""
    reached = _reach_upstream(run, citation.stage_id, citation.row_ordinal)
    reference = find_reference_inputs(run.stages)
    return [FrameScale(stage=sid, rows=run.rows[sid], covered=len(reached[sid]),
                       reference=sid in reference)
            for sid in run.order if sid in reached and run.rows[sid]]


def read_cut(run: RunBranches, outputs: Path, branch: BranchId,
             sample: int) -> CutRows | None:
    """The rows behind one branch: counts over all of them, cells over a sample."""
    at_stage, ordinals = find_rows_that_took(run, branch)
    if not ordinals or at_stage not in run.paths:
        return None
    paths, path_index = index_paths(run, at_stage, ordinals, set())
    spread = Counter(path_index)
    shown = ordinals[:sample]
    frame = read_frame_table(outputs / f"{at_stage}.parquet")
    return CutRows(
        branch=branch, at_stage=at_stage, total=len(ordinals), paths=paths,
        path_rows=[spread[i] for i in range(len(paths))],
        rows=read_rows(frame, shown, path_index[:len(shown)], None),
        stages=[entry for entry in run.read_branching_stages()
                if entry.id in _stages_touched(run, paths)],
    )


def find_rows_that_took(run: RunBranches, branch: BranchId
                        ) -> tuple[StageId, list[RowOrdinal]]:
    """Where a branch's rows live: a lost row is in the stage's INPUT frame."""
    fact = run.catalog[branch]
    if fact.origin is BranchOrigin.aggregate:
        return _find_group_members(run, branch)
    if fact.stage == SOURCE_STAGE:
        loader = branch.split("|", 1)[1]
        return loader, list(range(run.rows[loader]))
    arm = branch.split("|", 1)[1]
    if arm == DROPPED_ARM:
        return _find_lost_rows(run, fact.stage, arm)
    return fact.stage, [i for i, held in enumerate(run.paths[fact.stage])
                        if branch in held]


def index_paths(run: RunBranches, at_stage: StageId, ordinals: list[RowOrdinal],
                on_route: set[StageId]) -> tuple[list[BranchPath], list[int]]:
    """Distinct paths, and one small int per row. See docs/scope-map.md on `on_route`."""
    paths: list[BranchPath] = []
    seen: dict[BranchPath, int] = {}
    index = []
    for ordinal in ordinals:
        path = _on_route(run, run.paths[at_stage][ordinal], on_route)
        if path not in seen:
            seen[path] = len(paths)
            paths.append(path)
        index.append(seen[path])
    return paths, index


def read_rows(frame: pa.Table, ordinals: list[RowOrdinal], path_index: list[int],
              value_column: str | None) -> list[ContributingRow]:
    cells = {name: frame.column(name).to_pylist() for name in frame.column_names}
    gives = cells.get(value_column) if value_column else None
    return [ContributingRow(
        ordinal=ordinal, path=path_index[position],
        contribution=_plain(gives[ordinal]) if gives is not None else None,
        cells={name: _plain(column[ordinal]) for name, column in cells.items()},
    ) for position, ordinal in enumerate(ordinals)]


# ─── walking down to the rows ────────────────────────────────────────────────

def _expand(run: RunBranches, stage_id: StageId, ordinal: RowOrdinal
            ) -> tuple[list[RowOrdinal], StageId, list[StageId]]:
    fed_by = _contributors(run, stage_id, ordinal)
    if fed_by is None:
        return [ordinal], stage_id, []
    if not fed_by:
        return [], stage_id, []
    gathered: list[RowOrdinal] = []
    landed: set[StageId] = set()
    below: list[StageId] = []
    for parent in fed_by:
        rows, at_stage, deeper = _expand(run, parent.stage_id, parent.row_ordinal)
        gathered.extend(rows)
        landed.add(at_stage)
        below = deeper
    if len(landed) > 1:
        raise UnresolvableFigure(
            f"{stage_id} row {ordinal} bottoms out in {sorted(landed)}; "
            f"a set of contributing rows must sit at one grain")
    return gathered, landed.pop(), [stage_id] + below


def _contributors(run: RunBranches, stage_id: StageId, ordinal: RowOrdinal):
    """None when the row is its own row set; a list when it is a group."""
    lineage = run.lineages.get(stage_id)
    if lineage is None or ordinal >= len(lineage.parents):
        return None
    fed_by = [p for p in lineage.parents[ordinal] if p.kind == CONTRIBUTION]
    return fed_by or None


def _reach_upstream(run: RunBranches, stage_id: StageId, ordinal: RowOrdinal
                    ) -> dict[StageId, set[RowOrdinal]]:
    """Every (stage, row) the cited row descends from, ignoring what each hop meant."""
    found: dict[StageId, set[RowOrdinal]] = {}
    front = [(stage_id, ordinal)]
    seen = {(stage_id, ordinal)}
    while front:
        sid, row = front.pop()
        found.setdefault(sid, set()).add(row)
        for step in _one_hop_up(run, sid, row):
            if step not in seen:
                seen.add(step)
                front.append(step)
    return found


def _one_hop_up(run: RunBranches, sid: StageId, row: RowOrdinal
                ) -> list[tuple[StageId, RowOrdinal]]:
    lineage = run.lineages.get(sid)
    if lineage is not None and row < len(lineage.parents):
        return [(p.stage_id, p.row_ordinal) for p in lineage.parents[row]]
    stage = run.stages.get(sid)
    inputs = [ref.id for ref in stage.inputs] if stage else []
    if len(inputs) == 1 and run.rows[inputs[0]] == run.rows[sid]:
        return [(inputs[0], row)]  # row-preserving: output i is input i
    return []


# ─── what the figure's formula does with those rows ──────────────────────────

def _totals_by_adding(run: RunBranches, citation: StageOutputCellCitation,
                      through: list[StageId]) -> bool:
    """Values total to the figure only where this frame is what the formula read."""
    if len(through) > 1:
        return False
    return _read_formula(run, citation) == AggFormula.sum.value


def _read_formula(run: RunBranches, citation: StageOutputCellCitation) -> str | None:
    found = _find_aggregation(run, citation)
    if found is None:
        return None
    return str(getattr(found.formula, "value", found.formula))


def _read_value_column(run: RunBranches, citation: StageOutputCellCitation) -> str | None:
    found = _find_aggregation(run, citation)
    return found.value_column if found is not None else None


def _find_aggregation(run: RunBranches, citation: StageOutputCellCitation):
    stage = run.stages.get(citation.stage_id)
    block = getattr(stage.stage, "aggregate", None) if stage else None
    for aggregation in getattr(block, "aggregations", None) or []:
        if aggregation.output_column == citation.column:
            return aggregation
    return None


# ─── the pieces the page draws ───────────────────────────────────────────────

def _on_route(run: RunBranches, path: BranchPath, on_route: set[StageId]) -> BranchPath:
    return tuple(branch for branch in path
                 if run.catalog[branch].origin is not BranchOrigin.aggregate
                 or run.catalog[branch].stage in on_route)


def _branches_on(run: RunBranches, paths: list[BranchPath]) -> dict:
    touched = _stages_touched(run, paths)
    return {branch: fact for branch, fact in run.catalog.items()
            if any(branch in path for path in paths) or fact.stage in touched}


def _stages_touched(run: RunBranches, paths: list[BranchPath]) -> set[StageId]:
    return {run.catalog[branch].stage for path in paths for branch in path}


def _count_reach(run: RunBranches, paths, path_index) -> list[BranchReach]:
    here: Counter[BranchId] = Counter()
    for index in path_index:
        here.update(paths[index])
    touched = _stages_touched(run, paths)
    return [BranchReach(branch=branch, taken=taken, here=here[branch])
            for branch, taken in run.taken.items()
            if run.catalog[branch].stage in touched]


def _find_group_members(run: RunBranches, branch: BranchId
                        ) -> tuple[StageId, list[RowOrdinal]]:
    for sid, rows in run.groups.items():
        members = sorted(row for row, held in rows.items() if branch in held)
        if members:
            return sid, members
    return run.catalog[branch].stage, []


def _find_lost_rows(run: RunBranches, stage_id: StageId, arm: str
                    ) -> tuple[StageId, list[RowOrdinal]]:
    stage = run.stages[stage_id]
    parent = stage.inputs[0].id
    lineage = run.lineages[stage_id]
    if lineage is None:
        return parent, []
    kept = {p.row_ordinal for group in lineage.parents for p in group
            if p.stage_id == parent}
    return parent, [i for i in range(run.rows[parent]) if i not in kept]


def _plain(value: object) -> ScalarCell:
    plain = value.as_py() if hasattr(value, "as_py") else value
    if isinstance(plain, float) and plain != plain:
        return None
    if isinstance(plain, (str, int, float, bool)) or plain is None:
        return plain
    return str(plain)


def is_drawn_as_a_cut(role: BranchRole) -> bool:
    """An untaken `arm`'s rows carried on; only these two left the figure here."""
    return role in (BranchRole.removes, BranchRole.excludes)
