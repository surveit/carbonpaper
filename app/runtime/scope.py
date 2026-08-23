"""Read one run's branches back off its sidecars: what told each row apart.

Self-contained on the run directory, like `app.runtime.trace`.
See docs/scope-map.md.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.core.branch_source import find_branches
from app.core.frames import read_frame_table
from app.models.scope_map import (
    BranchFact,
    BranchId,
    BranchingStage,
    BranchOrigin,
    BranchPath,
    BranchRole,
    RowOrdinal,
    StageId,
)
from app.models.stage import StageType
from app.models.workflow_stage import WorkflowStage
from app.runtime.branches import RowBranches, branch_sidecar_path
from app.runtime.lineage import EdgeKind, RowLineage, lineage_sidecar_path

SOURCE_STAGE = "(source)"
CONTRIBUTION = EdgeKind.contribution.value
_LOOKS_UP = (StageType.enrich, StageType.expand)


@dataclass
class RunBranches:
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

    def read_branching_stages(self) -> list[BranchingStage]:
        branching = {fact.stage for fact in self.catalog.values()}
        return [_source_entry()] + [
            read_branching_stage(self.stages[sid], position)
            for position, sid in enumerate(self.order)
            if sid in branching and sid in self.stages
        ]


def read_run_branches(
    run_dir: Path, stages: dict[StageId, WorkflowStage], order: list[StageId],
    rows: dict[StageId, int],
) -> RunBranches:
    lineages = {sid: _read_lineage(run_dir, sid) for sid in order}
    recorded = {sid: taken for sid in order
                if (taken := _read_taken(run_dir, sid)) is not None}
    implied = {sid: _find_implied(stages.get(sid), lineages[sid], rows) for sid in order}
    groups, group_facts = _find_groups(order, lineages, stages)

    catalog = _catalog_code(stages, recorded)
    for found in implied.values():
        catalog.update(found.catalog)
    catalog.update(group_facts)
    return RunBranches(
        catalog=catalog,
        paths=_carry_forward(order, stages, lineages, rows, recorded, implied, groups,
                             _rank_branches(order, catalog)),
        taken=_count_taken(recorded, implied, groups),
        groups=groups, lineages=lineages, stages=stages, order=order, rows=rows,
    )


def read_branching_stage(stage: WorkflowStage, position: int) -> BranchingStage:
    authored = stage.stage
    return BranchingStage(
        id=authored.id, type=str(getattr(authored.type, "value", authored.type)),
        position=position,
        description=authored.description or "",
        code=read_stage_code(stage) or read_decision_source(stage),
    )


def is_a_dedupe(stage: WorkflowStage | None) -> bool:
    return stage is not None and stage.stage.type == StageType.dedupe


def find_reference_inputs(stages: dict[StageId, WorkflowStage]) -> set[StageId]:
    """A join's inputs[1:] are lookup tables; a union's inputs are all subjects."""
    return {ref.id for stage in stages.values() if stage.stage.type in _LOOKS_UP
            for ref in stage.inputs[1:]}


# ─── branches the stage never wrote an `if` for ──────────────────────────────

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
    if is_a_dedupe(stage) and lineage is not None:
        return _mark_duplicates(stage, lineage)
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
    fact = BranchFact(id=branch, stage=SOURCE_STAGE, origin=BranchOrigin.load,
                      role=BranchRole.arm, label=f"loaded by {stage.id}",
                      source=stage.stage.description or "")
    return _Implied([(branch,)] * rows, catalog={branch: fact})


def _mark_duplicates(stage: WorkflowStage, lineage: RowLineage) -> _Implied:
    kept, gone = f"{stage.id}|kept", f"{stage.id}|duplicate"
    found = _Implied([(kept,) for _ in lineage.parents])
    found.catalog[kept] = _implied_fact(stage, kept, BranchOrigin.predicate,
                                        "kept, one row per group")
    found.catalog[gone] = _implied_fact(stage, gone, BranchOrigin.predicate,
                                        "discarded as a duplicate")
    discarded = sum(1 for entry in lineage.parents for p in entry
                    if p.kind == CONTRIBUTION)
    if discarded:
        found.unreached[gone] = discarded
    return found


def _is_a_partition(present: dict[StageId, list[bool]], inputs: list[StageId]) -> bool:
    """A union hands each row to exactly one input; a join can reach two."""
    if len(inputs) < 2:
        return False
    return all(sum(present[ref][i] for ref in inputs) == 1
               for i in range(len(present[inputs[0]])))


def _mark_which_input(found: _Implied, stage: WorkflowStage, present) -> None:
    for ref, flags in present.items():
        branch = f"{stage.id}|from:{ref}"
        found.catalog[branch] = _implied_fact(stage, branch, BranchOrigin.union,
                                              f"came from {ref}")
        _mark(found, flags, branch)


def _mark_join_misses(found: _Implied, stage: WorkflowStage, present) -> None:
    for ref, flags in present.items():
        hits = sum(flags)
        if not 0 < hits < len(flags):
            continue  # every row matched, or none did: no distinction to draw
        hit, miss = f"{stage.id}|matched:{ref}", f"{stage.id}|missed:{ref}"
        found.catalog[hit] = _implied_fact(stage, hit, BranchOrigin.lookup,
                                           f"matched a row in {ref}")
        found.catalog[miss] = _implied_fact(stage, miss, BranchOrigin.lookup,
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
        found.catalog[kept] = _implied_fact(stage, kept, BranchOrigin.predicate,
                                            "kept by the predicate")
        found.catalog[gone] = _implied_fact(stage, gone, BranchOrigin.predicate,
                                            "dropped by the predicate")
        _mark(found, flags, kept)
        found.unreached[gone] = dropped


def _mark(found: _Implied, flags: list[bool], branch: BranchId) -> None:
    found.per_row = [held + (branch,) if flag else held
                     for held, flag in zip(found.per_row, flags)]


_REMOVES = ("dropped", "duplicate")


def _implied_fact(stage: WorkflowStage, branch: BranchId, origin: BranchOrigin,
                  label: str) -> BranchFact:
    arm = branch.split("|", 1)[1]
    return BranchFact(
        id=branch, stage=stage.id, origin=origin,
        role=BranchRole.removes if arm in _REMOVES else BranchRole.arm,
        label=label, source=read_decision_source(stage),
    )


# ─── which group of an aggregate a row fed ───────────────────────────────────

def _find_groups(order: list[StageId], lineages: dict[StageId, RowLineage | None],
                 stages: dict[StageId, WorkflowStage],
                 ) -> tuple[dict[StageId, dict[RowOrdinal, tuple[BranchId, ...]]],
                            dict[BranchId, BranchFact]]:
    held: dict[StageId, dict[RowOrdinal, list[BranchId]]] = {}
    catalog: dict[BranchId, BranchFact] = {}
    for sid in order:
        lineage = lineages.get(sid)
        if lineage is None or is_a_dedupe(stages.get(sid)):
            continue
        for ordinal, parents in enumerate(lineage.parents):
            for parent in parents:
                if parent.kind != CONTRIBUTION:
                    continue
                branch = f"{sid}|group:{ordinal}"
                held.setdefault(parent.stage_id, {}).setdefault(
                    parent.row_ordinal, []).append(branch)
                catalog[branch] = BranchFact(
                    id=branch, stage=sid, origin=BranchOrigin.aggregate,
                    role=BranchRole.excludes, label=f"fed group {ordinal}")
    return ({sid: {row: tuple(dict.fromkeys(found)) for row, found in rows.items()}
             for sid, rows in held.items()}, catalog)


# ─── carrying the branches forward ───────────────────────────────────────────

def _carry_forward(order, stages, lineages, rows, recorded, implied, groups, rank
                   ) -> dict[StageId, list[BranchPath]]:
    paths: dict[StageId, list[BranchPath]] = {}
    for sid in order:
        inherited = _inherit(sid, stages.get(sid), lineages[sid], rows, paths)
        own = recorded.get(sid)
        here = own.taken if own is not None else [()] * len(inherited)
        fed = groups.get(sid, {})
        paths[sid] = [
            tuple(sorted(set(carried) | set(_code_branches(sid, cell)) | set(extra)
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


def _code_branches(sid: StageId, cell) -> list[BranchId]:
    return [f"{sid}|{arm}" for arm in dict.fromkeys(cell or ())]


def _rank_branches(order: list[StageId], catalog) -> dict[BranchId, tuple]:
    """One order for a row's branches, so two rows holding the same set compare equal."""
    return {branch: (_position(order, fact.stage), fact.decided_at or 0, branch)
            for branch, fact in catalog.items()}


def _count_taken(recorded, implied, groups) -> Counter[BranchId]:
    taken: Counter[BranchId] = Counter(
        branch for sid in recorded for cell in recorded[sid].taken
        for branch in _code_branches(sid, cell))
    for found in implied.values():
        taken.update(b for held in found.per_row for b in held)
        taken.update(found.unreached)
    taken.update(b for rows in groups.values() for held in rows.values() for b in held)
    return taken


# ─── the code behind a branch ────────────────────────────────────────────────

def _catalog_code(stages, recorded) -> dict[BranchId, BranchFact]:
    catalog: dict[BranchId, BranchFact] = {}
    for sid in recorded:
        source = read_stage_code(stages.get(sid))
        lines = source.split("\n")
        for branch in find_branches(source):
            tested_at, label = _read_test(lines, branch)
            catalog[f"{sid}|{branch.id}"] = BranchFact(
                id=f"{sid}|{branch.id}", stage=sid, origin=BranchOrigin.code,
                role=BranchRole.arm, label=label, source=lines[branch.line - 1].strip(),
                tested_at=tested_at, decided_at=branch.line)
    return catalog


_OPENERS = ("if", "elif", "else", "try", "except")


def _read_test(lines: list[str], branch) -> tuple[int, str]:
    """`Branch.line` is the body's first statement; the test the row passed is above it."""
    prefix = lines[branch.line - 1][:branch.column]
    if prefix.strip():
        return branch.line, prefix.strip()  # `if x: y = 1`
    last = branch.line - 2
    while last > 0 and not lines[last].strip():
        last -= 1
    first = last
    while first > 0 and not _opens_a_branch(lines[first]):
        first -= 1
    if not _opens_a_branch(lines[first]):
        return last + 1, lines[last].strip()
    return first + 1, " ".join(line.strip() for line in lines[first:last + 1])


def _opens_a_branch(line: str) -> bool:
    head = line.strip().split("(")[0].split(":")[0].split()
    return bool(head) and head[0] in _OPENERS


def read_stage_code(stage: WorkflowStage | None) -> str:
    for holder in ("starlark", "function", "filter"):
        block = getattr(stage.stage, holder, None) if stage else None
        if block is not None and getattr(block, "code", None):
            return str(block.code)
    return ""


def read_decision_source(stage: WorkflowStage) -> str:
    """The filter's code, the join's key pairs, or the dedupe's keys and tie-break."""
    authored = stage.stage
    predicate = getattr(authored, "filter", None)
    if predicate is not None and getattr(predicate, "code", None):
        return str(predicate.code)
    join = getattr(authored, "join", None)
    if join is not None:
        return "\n".join(f"{pair.left} == {pair.right}" for pair in join.keys)
    duplicates = getattr(authored, "dedupe", None)
    if duplicates is not None:
        keep = getattr(duplicates.keep, "value", duplicates.keep)
        return f"keys: {', '.join(duplicates.keys)}\nkeep: {keep}"
    return ""


# ─── reading the run ─────────────────────────────────────────────────────────

def _read_taken(run_dir: Path, stage_id: StageId) -> RowBranches | None:
    path = branch_sidecar_path(run_dir, stage_id)
    return RowBranches.from_table(read_frame_table(path)) if path.exists() else None


def _read_lineage(run_dir: Path, stage_id: StageId) -> RowLineage | None:
    path = lineage_sidecar_path(run_dir, stage_id)
    return RowLineage.from_table(read_frame_table(path)) if path.exists() else None


def _position(order: list[StageId], stage_id: StageId) -> int:
    return order.index(stage_id) if stage_id in order else -1


def _source_entry() -> BranchingStage:
    return BranchingStage(id=SOURCE_STAGE, type="input_data", position=-1,
                          description="which stage read this row off disk")
