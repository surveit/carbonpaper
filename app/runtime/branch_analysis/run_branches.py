"""Which branches each row of a run holds. See docs/branch-analysis.md."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.runtime.errors import MissingLineage, NotALoadStage
from app.models.branch_analysis import (
    BranchId,
    BranchOption,
    BranchPath,
    BranchReason,
    BranchRole,
    RowOrdinal,
)
from app.models.schema import StageId
from app.models.stage import StageType, is_grain_and_order_preserving
from app.models.workflow_stage import WorkflowStage
from app.runtime.branch_analysis.stage_code import (
    find_code_branches,
    read_decision_source,
)
from app.runtime.branches import RowBranches
from app.runtime.lineage import EdgeKind, RowLineage, RowParent
from app.runtime.lineage_sidecar import read_lineage_sidecar

MERGE_EDGE = EdgeKind.contribution.value
_LOOKS_UP = (StageType.enrich, StageType.expand)

BranchesPerRow = list[tuple[BranchId, ...]]
LineagePerStage = Mapping[StageId, RowLineage | None]
RowsReachingEachInput = dict[StageId, list[bool]]
MergesPerRow = Mapping[StageId, Mapping[RowOrdinal, tuple[BranchId, ...]]]


@dataclass
class WorkflowRunBranches:
    """Every branch the run recorded, and the ones each row of each stage holds."""

    branch_options: dict[BranchId, BranchOption]
    branch_paths: Mapping[StageId, list[BranchPath]]
    # How many rows took each branch, run-wide. Free forward, invisible backward.
    row_count_per_branch_id: Counter[BranchId]
    merges_per_row: MergesPerRow
    lineages: LineagePerStage
    stages: dict[StageId, WorkflowStage]
    ordered_stage_ids: list[StageId]
    row_counts: dict[StageId, int]

    def find_branching_stage_ids(self) -> set[StageId]:
        return {option.stage_id for option in self.branch_options.values()}


def reconstruct_run_branches(
    run_dir: Path, stages: dict[StageId, WorkflowStage],
    ordered_stage_ids: list[StageId], row_counts: dict[StageId, int],
) -> WorkflowRunBranches:
    sidecars = {sid: read_lineage_sidecar(run_dir, sid) for sid in ordered_stage_ids}
    lineages = {sid: sidecars[sid].lineage for sid in ordered_stage_ids}
    arms_taken = {sid: sidecar.branches for sid, sidecar in sidecars.items()
                  if sidecar.branches is not None}
    from_lineage = {
        sid: _read_branching_from_lineage(stages.get(sid), lineages[sid], row_counts)
        for sid in ordered_stage_ids}
    merges_per_row, merge_options = enumerate_merges(ordered_stage_ids, lineages)

    options = find_code_branches(stages, arms_taken)
    for read in from_lineage.values():
        options.update(read.options)
    options.update(merge_options)
    return WorkflowRunBranches(
        branch_options=options,
        branch_paths=build_branch_path_per_row(
            ordered_stage_ids, stages, lineages, row_counts, arms_taken, from_lineage,
            merges_per_row, _rank_branches(ordered_stage_ids, options)),
        row_count_per_branch_id=_count_rows_per_branch(arms_taken, from_lineage,
                                                       merges_per_row),
        merges_per_row=merges_per_row, lineages=lineages, stages=stages,
        ordered_stage_ids=ordered_stage_ids, row_counts=row_counts,
    )


def find_subject_inputs(stage: WorkflowStage) -> list[StageId]:
    """The inputs a stage's rows come FROM: a join's inputs[0], every input otherwise."""
    refs = [ref.id for ref in stage.inputs]
    return refs[:1] if stage.stage.type in _LOOKS_UP else refs


def find_reference_inputs(stages: dict[StageId, WorkflowStage]) -> set[StageId]:
    """A join's inputs[1:] are lookup tables; a union's inputs are all subjects."""
    return {ref.id for stage in stages.values() if stage.stage.type in _LOOKS_UP
            for ref in stage.inputs[1:]}


# ─── branches no stage wrote an `if` for ─────────────────────────────────────

@dataclass
class BranchingReadFromLineage:
    """What one stage's lineage says about how it told its rows apart."""

    per_row: BranchesPerRow = field(default_factory=list)
    # Branches with no surviving row to carry them: a filter's removed side.
    rows_with_no_survivor: dict[BranchId, int] = field(default_factory=dict)
    options: dict[BranchId, BranchOption] = field(default_factory=dict)


def _read_branching_from_lineage(
    stage: WorkflowStage | None, lineage: RowLineage | None,
    row_counts: dict[StageId, int],
) -> BranchingReadFromLineage:
    if stage is None:
        return BranchingReadFromLineage()
    input_stage_ids = [ref.id for ref in stage.inputs]
    if not input_stage_ids:
        return _read_the_load(stage, row_counts[stage.id])
    if lineage is None:
        _refuse_a_gap_in_the_lineage(stage)
        return BranchingReadFromLineage([()] * row_counts[stage.id])
    read = BranchingReadFromLineage([() for _ in lineage.parents])
    reaching = _find_rows_reaching_each_input(lineage, input_stage_ids)
    if len(input_stage_ids) > 1 and _every_row_reaches_one_input(reaching):
        _read_which_input(read, stage, reaching)
    else:
        _read_join_misses(read, stage, reaching)
    _read_removals(read, stage, lineage, reaching, row_counts)
    return read


def _refuse_a_gap_in_the_lineage(stage: WorkflowStage) -> None:
    """Absent lineage is a row-preserving type's contract, or a report. Never a gap."""
    if is_grain_and_order_preserving(stage.stage.type):
        return
    if stage.stage.type == StageType.report:
        return
    raise MissingLineage(
        f"stage '{stage.id}' is a {_name_the_type(stage)}, which neither preserves "
        f"its rows nor reports, so it owed a lineage sidecar and wrote none")


def _name_the_type(stage: WorkflowStage) -> str:
    return str(getattr(stage.stage.type, "value", stage.stage.type))


def _read_the_load(stage: WorkflowStage, rows: int) -> BranchingReadFromLineage:
    if stage.stage.type != StageType.input_data:
        raise NotALoadStage(
            f"stage '{stage.id}' is a {_name_the_type(stage)} with no inputs; only "
            f"an input_data stage reads rows off disk")
    branch_id = f"{stage.id}|loaded"
    option = BranchOption(
        id=branch_id, stage_id=stage.id, rows_live_in_stage_id=stage.id,
        reason=BranchReason.load, role=BranchRole.keeps,
        label=f"loaded by {stage.id}", source_code=stage.stage.description or "")
    return BranchingReadFromLineage([(branch_id,)] * rows, options={branch_id: option})


def _find_rows_reaching_each_input(lineage: RowLineage,
                                   input_stage_ids: list[StageId]
                                   ) -> RowsReachingEachInput:
    return {input_stage_id: [any(p.stage_id == input_stage_id for p in entry)
                             for entry in lineage.parents]
            for input_stage_id in input_stage_ids}


def _every_row_reaches_one_input(reaching: RowsReachingEachInput) -> bool:
    """A union hands each row to exactly one input; a join can reach two."""
    rows = len(next(iter(reaching.values())))
    return all(sum(flags[row] for flags in reaching.values()) == 1
               for row in range(rows))


def _read_which_input(read: BranchingReadFromLineage, stage: WorkflowStage,
                      reaching: RowsReachingEachInput) -> None:
    for input_stage_id, flags in reaching.items():
        branch_id = f"{stage.id}|from:{input_stage_id}"
        read.options[branch_id] = _option(
            stage, branch_id, BranchReason.union, BranchRole.keeps,
            f"came from {input_stage_id}", stage.id)
        _hold(read, flags, branch_id)


def _read_join_misses(read: BranchingReadFromLineage, stage: WorkflowStage,
                      reaching: RowsReachingEachInput) -> None:
    for input_stage_id, flags in reaching.items():
        hits = sum(flags)
        if not 0 < hits < len(flags):
            continue  # every row matched, or none did: no distinction to draw
        hit = f"{stage.id}|matched:{input_stage_id}"
        miss = f"{stage.id}|missed:{input_stage_id}"
        read.options[hit] = _option(stage, hit, BranchReason.join, BranchRole.keeps,
                                    f"matched a row in {input_stage_id}", stage.id)
        read.options[miss] = _option(stage, miss, BranchReason.join, BranchRole.keeps,
                                     f"no match in {input_stage_id}", stage.id)
        _hold(read, flags, hit)
        _hold(read, [not flag for flag in flags], miss)


def _read_removals(read: BranchingReadFromLineage, stage: WorkflowStage,
                   lineage: RowLineage, reaching: RowsReachingEachInput,
                   row_counts: dict[StageId, int]) -> None:
    """An input every row reaches is the spine; its rows nothing reaches were removed."""
    for input_stage_id, flags in reaching.items():
        if not all(flags):
            continue
        reached = {p.row_ordinal for entry in lineage.parents for p in entry
                   if p.stage_id == input_stage_id}
        removed = row_counts[input_stage_id] - len(reached)
        if removed <= 0:
            continue
        kept, gone = f"{stage.id}|kept", f"{stage.id}|removed"
        keep_label, remove_label = _name_the_removal(stage)
        read.options[kept] = _option(stage, kept, BranchReason.predicate,
                                     BranchRole.keeps, keep_label, stage.id)
        read.options[gone] = _option(stage, gone, BranchReason.predicate,
                                     BranchRole.removes, remove_label, input_stage_id)
        _hold(read, flags, kept)
        read.rows_with_no_survivor[gone] = removed


def _name_the_removal(stage: WorkflowStage) -> tuple[str, str]:
    # A dedupe removes rows the way a filter does, but it has keys, not a predicate.
    if stage.stage.type == StageType.dedupe:
        return "kept, one row per key", "dropped as a repeat of a kept row"
    return "kept by the predicate", "dropped by the predicate"


def _hold(read: BranchingReadFromLineage, flags: list[bool],
          branch_id: BranchId) -> None:
    read.per_row = [path + (branch_id,) if flag else path
                    for path, flag in zip(read.per_row, flags)]


def _option(stage: WorkflowStage, branch_id: BranchId, reason: BranchReason,
            role: BranchRole, label: str, rows_live_in: StageId) -> BranchOption:
    return BranchOption(
        id=branch_id, stage_id=stage.id, rows_live_in_stage_id=rows_live_in,
        reason=reason, role=role, label=label,
        source_code=read_decision_source(stage))


# ─── which rows a stage merged into one ──────────────────────────────────────

def enumerate_merges(ordered_stage_ids: list[StageId], lineages: LineagePerStage
                     ) -> tuple[MergesPerRow, dict[BranchId, BranchOption]]:
    held: dict[StageId, dict[RowOrdinal, list[BranchId]]] = {}
    options: dict[BranchId, BranchOption] = {}
    for sid in ordered_stage_ids:
        lineage = lineages.get(sid)
        if lineage is None:
            continue
        for ordinal, parents in enumerate(lineage.parents):
            for parent in parents:
                if parent.kind != MERGE_EDGE:
                    continue
                branch_id = f"{sid}|merged:{ordinal}"
                held.setdefault(parent.stage_id, {}).setdefault(
                    parent.row_ordinal, []).append(branch_id)
                options[branch_id] = _merge_option(sid, branch_id, ordinal, parent)
    return ({sid: {row: tuple(dict.fromkeys(found)) for row, found in rows.items()}
             for sid, rows in held.items()}, options)


def _merge_option(sid: StageId, branch_id: BranchId, ordinal: RowOrdinal,
                  parent: RowParent) -> BranchOption:
    return BranchOption(
        id=branch_id, stage_id=sid, rows_live_in_stage_id=parent.stage_id,
        reason=BranchReason.merge, role=BranchRole.keeps,
        merged_into_row_ordinal=ordinal)


# ─── one path per row, out of the per-stage branches ─────────────────────────

def build_branch_path_per_row(
    ordered_stage_ids: list[StageId], stages: dict[StageId, WorkflowStage],
    lineages: LineagePerStage, row_counts: dict[StageId, int],
    arms_taken: dict[StageId, RowBranches],
    from_lineage: dict[StageId, BranchingReadFromLineage],
    merges_per_row: MergesPerRow,
    rank: dict[BranchId, tuple[int, int, str]],
) -> dict[StageId, list[BranchPath]]:
    paths: dict[StageId, list[BranchPath]] = {}
    for sid in ordered_stage_ids:
        inherited = _inherit(sid, stages.get(sid), lineages[sid], row_counts, paths)
        recorded = arms_taken.get(sid)
        here = recorded.taken if recorded is not None else [()] * len(inherited)
        merged = merges_per_row.get(sid, {})
        paths[sid] = [
            tuple(sorted(set(carried) | set(_name_arms(sid, cell)) | set(read)
                         | set(merged.get(row, ())),
                         key=lambda b: rank.get(b, (len(ordered_stage_ids), 0, b))))
            for row, (carried, cell, read)
            in enumerate(zip(inherited, here, from_lineage[sid].per_row))
        ]
    return paths


def _inherit(sid: StageId, stage: WorkflowStage | None, lineage: RowLineage | None,
             row_counts: dict[StageId, int],
             paths: dict[StageId, list[BranchPath]]) -> list[BranchPath]:
    if lineage is not None:
        return [_join_parent_paths(paths, entry) for entry in lineage.parents]
    # No lineage: the stage type's contract says output row i IS input row i.
    inputs = [ref.id for ref in stage.inputs] if stage else []
    if len(inputs) == 1 and stage and is_grain_and_order_preserving(stage.stage.type):
        return list(paths[inputs[0]])
    return [()] * row_counts[sid]


def _join_parent_paths(paths: dict[StageId, list[BranchPath]],
                       parents: list[RowParent]) -> BranchPath:
    carried = [paths[p.stage_id][p.row_ordinal] for p in parents
               if p.row_ordinal < len(paths.get(p.stage_id) or [])]
    return tuple(dict.fromkeys(b for path in carried for b in path))


def _name_arms(sid: StageId, cell: tuple[str, ...] | None) -> list[BranchId]:
    return [f"{sid}|{arm}" for arm in dict.fromkeys(cell or ())]


def _rank_branches(ordered_stage_ids: list[StageId],
                   options: dict[BranchId, BranchOption]
                   ) -> dict[BranchId, tuple[int, int, str]]:
    """One order for a row's branches, so two rows holding the same set compare equal."""
    return {branch_id: (find_stage_position(ordered_stage_ids, option.stage_id),
                        option.first_body_line_number or 0, branch_id)
            for branch_id, option in options.items()}


def _count_rows_per_branch(
    arms_taken: dict[StageId, RowBranches],
    from_lineage: dict[StageId, BranchingReadFromLineage],
    merges_per_row: MergesPerRow,
) -> Counter[BranchId]:
    counted: Counter[BranchId] = Counter(
        branch_id for sid in arms_taken for cell in arms_taken[sid].taken
        for branch_id in _name_arms(sid, cell))
    for read in from_lineage.values():
        counted.update(b for path in read.per_row for b in path)
        counted.update(read.rows_with_no_survivor)
    counted.update(b for rows in merges_per_row.values()
                   for path in rows.values() for b in path)
    return counted


# ─── reading the run ─────────────────────────────────────────────────────────

def find_stage_position(ordered_stage_ids: list[StageId], stage_id: StageId) -> int:
    return (ordered_stage_ids.index(stage_id) if stage_id in ordered_stage_ids
            else len(ordered_stage_ids))
