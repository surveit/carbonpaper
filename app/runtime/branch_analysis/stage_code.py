"""The stage source a branch decided in, and the branch facts read out of it."""

from __future__ import annotations

from app.core.branch_source import find_branches, read_branch_test
from app.models.branch_analysis import BranchFact, BranchId, BranchReason, BranchRole
from app.models.workflow_stage import WorkflowStage


def find_code_branches(stages, recorded) -> dict[BranchId, BranchFact]:
    catalog: dict[BranchId, BranchFact] = {}
    for sid in recorded:
        source = read_stage_code(stages.get(sid))
        lines = source.split("\n")
        for branch in find_branches(source):
            tested_at, label = read_branch_test(lines, branch)
            catalog[f"{sid}|{branch.id}"] = BranchFact(
                id=f"{sid}|{branch.id}", stage=sid, reason=BranchReason.code,
                role=BranchRole.keeps, label=label, source=lines[branch.line - 1].strip(),
                tested_at=tested_at, decided_at=branch.line)
    return catalog

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
