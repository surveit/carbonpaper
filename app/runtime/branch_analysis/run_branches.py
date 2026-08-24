"""Which branches each row of a run holds. See docs/branch-analysis.md."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.core.frames import read_frame_table
from app.models.branch_analysis import (
    BranchFact,
    BranchId,
    BranchPath,
    BranchReason,
    BranchRole,
    RowOrdinal,
)
from app.models.schema import StageId
from app.models.stage import StageType
from app.models.workflow_stage import WorkflowStage
from app.runtime.branch_analysis.stage_code import (
    find_code_branches,
    read_decision_source,
)
from app.runtime.branches import RowBranches, branch_sidecar_path
from app.runtime.lineage import EdgeKind, RowLineage, lineage_sidecar_path

SOURCE_STAGE = "(source)"
CONTRIBUTION = EdgeKind.contribution.value
_LOOKS_UP = (StageType.enrich, StageType.expand)
_REMOVES = ("dropped",)


@dataclass
class WorkflowRunBranches:
    """Every branch the run recorded, and the ones each row of each stage holds."""

    catalog: dict[BranchId, BranchFact]
    paths: dict[StageId, list[BranchPath]]
    # How many rows took each branch, run-wide. Free forward, invisible backward.
    taken: Counter[BranchId]
    # `stage -> row -> the groups it fed`, for selecting an aggregate's contributors.
    groups: dict[StageId, dict[RowOrdinal, tuple[BranchId, ...]]]
    lineages: dict[StageId, RowLineage | None]
    stages: dict[StageId, WorkflowStage]
    order: list[StageId]
    rows: dict[StageId, int]

    def find_branching_stage_ids(self) -> set[StageId]:
        return {fact.stage for fact in self.catalog.values()}


def read_run_branches(
    run_dir: Path, stages: dict[StageId, WorkflowStage], order: list[StageId],
    rows: dict[StageId, int],
) -> WorkflowRunBranches:
    lineages = {sid: _read_lineage(run_dir, sid) for sid in order}
    recorded = {sid: taken for sid in order
                if (taken := _read_taken(run_dir, sid)) is not None}
    implied = {sid: _find_implied(stages.get(sid), lineages[sid], rows) for sid in order}
    groups, group_facts = _find_groups(order, lineages)

    catalog = find_code_branches(stages, recorded)
    for found in implied.values():
        catalog.update(found.catalog)
    catalog.update(group_facts)
    return WorkflowRunBranches(
        catalog=catalog,
        paths=_carry_forward(order, stages, lineages, rows, recorded, implied, groups,
                             _rank_branches(order, catalog)),
        taken=_count_taken(recorded, implied, groups),
        groups=groups, lineages=lineages, stages=stages, order=order, rows=rows,
    )

def find_reference_inputs(stages: dict[StageId, WorkflowStage]) -> set[StageId]:
    """A join's inputs[1:] are lookup tables; a union's inputs are all subjects."""
    return {ref.id for stage in stages.values() if stage.stage.type in _LOOKS_UP
            for ref in stage.inputs[1:]}

@dataclass
class _Implied:
    per_row: list[tuple[BranchId, ...]] = field(default_factory=list)
    # Arms with no surviving row to carry them: a filter's dropped side.
    unreached: dict[BranchId, int] = field(default_factory=dict)
    catalog: dict[BranchId, BranchFact] = field(default_factory=dict)

def _find_implied(stage: WorkflowStage | None, lineage: RowLineage | None,
                  rows: dict[StageId, int]) -> _Implied:
    if stage is None:
        return _Implied()
    inputs = [ref.id for ref in stage.inputs]
    if not inputs:
        return _mark_loaded_here(stage, rows[stage.id])
    if lineage is None:
        return _Implied([()] * rows[stage.id])
    found = _Implied([() for _ in lineage.parents])
    present = {ref: [any(p.stage_id == ref for p in entry) for entry in lineage.parents]
               for ref in inputs}
    if _is_a_partition(present, inputs):
        _mark_which_input(found, stage, present)
    else:
        _mark_join_misses(found, stage, present)
    _mark_drops(found, stage, lineage, present, rows)
    return found

def _mark_loaded_here(stage: WorkflowStage, rows: int) -> _Implied:
    branch = f"{SOURCE_STAGE}|{stage.id}"
    fact = BranchFact(id=branch, stage=SOURCE_STAGE, reason=BranchReason.load,
                      role=BranchRole.keeps, label=f"loaded by {stage.id}",
                      source=stage.stage.description or "")
    return _Implied([(branch,)] * rows, catalog={branch: fact})

def _is_a_partition(present: dict[StageId, list[bool]], inputs: list[StageId]) -> bool:
    """A union hands each row to exactly one input; a join can reach two."""
    if len(inputs) < 2:
        return False
    return all(sum(present[ref][i] for ref in inputs) == 1
               for i in range(len(present[inputs[0]])))

def _mark_which_input(found: _Implied, stage: WorkflowStage, present) -> None:
    for ref, flags in present.items():
        branch = f"{stage.id}|from:{ref}"
        found.catalog[branch] = _implied_fact(stage, branch, BranchReason.union,
                                              f"came from {ref}")
        _mark(found, flags, branch)

def _mark_join_misses(found: _Implied, stage: WorkflowStage, present) -> None:
    for ref, flags in present.items():
        hits = sum(flags)
        if not 0 < hits < len(flags):
            continue  # every row matched, or none did: no distinction to draw
        hit, miss = f"{stage.id}|matched:{ref}", f"{stage.id}|missed:{ref}"
        found.catalog[hit] = _implied_fact(stage, hit, BranchReason.join,
                                           f"matched a row in {ref}")
        found.catalog[miss] = _implied_fact(stage, miss, BranchReason.join,
                                            f"no match in {ref}")
        _mark(found, flags, hit)
        _mark(found, [not flag for flag in flags], miss)

def _mark_drops(found: _Implied, stage: WorkflowStage, lineage: RowLineage, present,
                rows: dict[StageId, int]) -> None:
    """An input every row reaches is the spine; its rows nothing reaches were dropped."""
    for ref, flags in present.items():
        if not all(flags):
            continue
        seen = {p.row_ordinal for entry in lineage.parents for p in entry
                if p.stage_id == ref}
        dropped = rows[ref] - len(seen)
        if dropped <= 0:
            continue
        kept, gone = f"{stage.id}|kept", f"{stage.id}|dropped"
        keep_label, drop_label = _name_the_removal(stage)
        found.catalog[kept] = _implied_fact(stage, kept, BranchReason.predicate,
                                            keep_label)
        found.catalog[gone] = _implied_fact(stage, gone, BranchReason.predicate,
                                            drop_label)
        _mark(found, flags, kept)
        found.unreached[gone] = dropped

def _name_the_removal(stage: WorkflowStage) -> tuple[str, str]:
    # A dedupe removes rows the way a filter does, but it has keys, not a predicate.
    if stage.stage.type is StageType.dedupe:
        return "kept, one row per key", "dropped as a repeat of a kept row"
    return "kept by the predicate", "dropped by the predicate"

def _mark(found: _Implied, flags: list[bool], branch: BranchId) -> None:
    found.per_row = [held + (branch,) if flag else held
                     for held, flag in zip(found.per_row, flags)]

def _implied_fact(stage: WorkflowStage, branch: BranchId, reason: BranchReason,
                  label: str) -> BranchFact:
    removed = branch.split("|", 1)[1]
    return BranchFact(
        id=branch, stage=stage.id, reason=reason,
        role=BranchRole.removes if removed in _REMOVES else BranchRole.keeps,
        label=label, source=read_decision_source(stage),
    )

def _find_groups(order: list[StageId], lineages: dict[StageId, RowLineage | None],
                 ) -> tuple[dict[StageId, dict[RowOrdinal, tuple[BranchId, ...]]],
                            dict[BranchId, BranchFact]]:
    held: dict[StageId, dict[RowOrdinal, list[BranchId]]] = {}
    catalog: dict[BranchId, BranchFact] = {}
    for sid in order:
        lineage = lineages.get(sid)
        if lineage is None:
            continue
        for ordinal, parents in enumerate(lineage.parents):
            for parent in parents:
                if parent.kind != CONTRIBUTION:
                    continue
                branch = f"{sid}|group:{ordinal}"
                held.setdefault(parent.stage_id, {}).setdefault(
                    parent.row_ordinal, []).append(branch)
                catalog[branch] = BranchFact(
                    id=branch, stage=sid, reason=BranchReason.aggregate,
                    role=BranchRole.excludes, label=f"fed group {ordinal}")
    return ({sid: {row: tuple(dict.fromkeys(found)) for row, found in rows.items()}
             for sid, rows in held.items()}, catalog)

def _carry_forward(order, stages, lineages, rows, recorded, implied, groups, rank
                   ) -> dict[StageId, list[BranchPath]]:
    paths: dict[StageId, list[BranchPath]] = {}
    for sid in order:
        inherited = _inherit(sid, stages.get(sid), lineages[sid], rows, paths)
        own = recorded.get(sid)
        here = own.taken if own is not None else [()] * len(inherited)
        fed = groups.get(sid, {})
        paths[sid] = [
            tuple(sorted(set(carried) | set(_read_arms_taken(sid, cell)) | set(extra)
                         | set(fed.get(i, ())), key=lambda b: rank.get(b, (len(order), 0, b))))
            for i, (carried, cell, extra)
            in enumerate(zip(inherited, here, implied[sid].per_row))
        ]
    return paths

def _inherit(sid, stage, lineage, rows, paths) -> list[BranchPath]:
    if lineage is not None:
        return [_merge(paths, entry) for entry in lineage.parents]
    parents = [ref.id for ref in stage.inputs] if stage else []
    n = rows[sid]
    if len(parents) == 1 and len(paths.get(parents[0], [])) == n:
        return list(paths[parents[0]])  # row-preserving: output i is input i
    return [()] * n

def _merge(paths, parents) -> BranchPath:
    carried = [paths[p.stage_id][p.row_ordinal] for p in parents
               if p.row_ordinal < len(paths.get(p.stage_id) or [])]
    return tuple(dict.fromkeys(b for held in carried for b in held))

def _read_arms_taken(sid: StageId, cell) -> list[BranchId]:
    return [f"{sid}|{arm}" for arm in dict.fromkeys(cell or ())]

def _rank_branches(order: list[StageId], catalog) -> dict[BranchId, tuple]:
    """One order for a row's branches, so two rows holding the same set compare equal."""
    return {branch: (find_stage_position(order, fact.stage), fact.decided_at or 0, branch)
            for branch, fact in catalog.items()}

def _count_taken(recorded, implied, groups) -> Counter[BranchId]:
    taken: Counter[BranchId] = Counter(
        branch for sid in recorded for cell in recorded[sid].taken
        for branch in _read_arms_taken(sid, cell))
    for found in implied.values():
        taken.update(b for held in found.per_row for b in held)
        taken.update(found.unreached)
    taken.update(b for rows in groups.values() for held in rows.values() for b in held)
    return taken

def _read_taken(run_dir: Path, stage_id: StageId) -> RowBranches | None:
    path = branch_sidecar_path(run_dir, stage_id)
    return RowBranches.from_table(read_frame_table(path)) if path.exists() else None

def _read_lineage(run_dir: Path, stage_id: StageId) -> RowLineage | None:
    path = lineage_sidecar_path(run_dir, stage_id)
    return RowLineage.from_table(read_frame_table(path)) if path.exists() else None


def find_stage_position(order: list[StageId], stage_id: StageId) -> int:
    return order.index(stage_id) if stage_id in order else len(order)
