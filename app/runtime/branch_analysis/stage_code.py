"""The stage source a branch decided in, and the branch options read out of it."""

from __future__ import annotations

from app.core.branch_source import find_branches, read_branch_test
from app.models.branch_analysis import (
    BranchId,
    BranchOption,
    BranchReason,
    BranchRole,
)
from app.models.schema import StageId
from app.models.workflow_stage import WorkflowStage
from app.runtime.branches import RowBranches


def find_code_branches(stages: dict[StageId, WorkflowStage],
                       arms_taken: dict[StageId, RowBranches],
                       ) -> dict[BranchId, BranchOption]:
    options: dict[BranchId, BranchOption] = {}
    for sid in arms_taken:
        source = read_stage_code(stages.get(sid))
        lines = source.split("\n")
        for branch in find_branches(source):
            test_line, label = read_branch_test(lines, branch)
            branch_id = f"{sid}|{branch.id}"
            options[branch_id] = BranchOption(
                id=branch_id, stage_id=sid, rows_live_in_stage_id=sid,
                reason=BranchReason.code, role=BranchRole.keeps, label=label,
                source_code=lines[branch.line - 1].strip(),
                test_line_number=test_line,
                first_body_line_number=branch.line,
                last_body_line_number=branch.end_line or branch.line)
    return options


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
    repeats = getattr(authored, "dedupe", None)
    if repeats is not None:
        keep = getattr(repeats.keep, "value", repeats.keep)
        return f"keys: {', '.join(repeats.keys)}\nkeep: {keep}"
    return ""
