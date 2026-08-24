"""The analysis states the facts; every sentence a reader sees is written here."""

from __future__ import annotations

from app.runtime.errors import UnresolvableFigure
from app.models.branch_analysis import BranchId, BranchRole, FrameScale
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.models.workflow import Workflow
from app.runtime.branch_analysis import (
    WorkflowRunBranches,
    reconstruct_run_branches,
)
from app.services import run as run_service
from app.services.scope import find_lookup_table_stages
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.scope_payload import (
    CutRows,
    ScopeMap,
    build_scope_map,
    find_cuts_to_offer,
)


def load_scope_map(project_id: str, run_id: str, citation: StageOutputCellCitation
                   ) -> tuple[ScopeMap, dict[BranchId, CutRows], set[StageId]]:
    run_branches = _read_run(project_id, run_id)
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    scope = build_scope_map(run_branches, project_id, run_id, outputs, citation)
    cuts = find_cuts_to_offer(run_branches, outputs, scope)
    return scope, cuts, find_lookup_table_stages(run_branches)


def say_what_the_rows_answer(scope: ScopeMap) -> str:
    """One sentence under the figure: which rows the drawing is about."""
    covered = len(scope.covers.ordinals)
    if not covered:
        return "Nothing fed this cell, which is why it is null."
    where = f"{covered:,} row{'' if covered == 1 else 's'} of {scope.covers.at_stage}"
    merged_at = scope.covers.regrained_at[1:]
    if not merged_at:
        return f"Computed from {where}."
    return (f"Computed from {where}, merged at {' and '.join(merged_at)} before this "
            f"figure was taken.")


def say_how_much_is_off_screen(scale: list[FrameScale],
                               lookups: set[StageId]) -> str | None:
    """The widest frame the figure passed through, so a slice is not read as the whole."""
    flow = [step for step in scale if step.stage not in lookups]
    if not flow:
        return None
    widest = max(flow, key=lambda step: step.rows_count)
    if widest.included_rows_count >= widest.rows_count:
        return None
    share = widest.included_rows_count / widest.rows_count * 100
    printed = f"{share:.2f}%" if share < 0.5 else f"{share:.1f}%"
    return (f"{widest.included_rows_count:,} of the {widest.rows_count:,} rows at "
            f"{widest.stage} — {printed} of the widest frame this figure passed "
            f"through. The rest of that frame is not drawn.")


def narrow_the_funnel(scale: list[FrameScale],
                      lookups: set[StageId]) -> list[FrameScale]:
    """The narrowing, with a lookup table and a repeat of the step before it dropped."""
    steps: list[FrameScale] = []
    for step in (s for s in scale if s.stage not in lookups):
        here = (step.rows_count, step.included_rows_count)
        if not steps or (steps[-1].rows_count, steps[-1].included_rows_count) != here:
            steps.append(step)
    return steps


def say_why_rows_left(cut: CutRows, role: BranchRole) -> str:
    if role is BranchRole.removes:
        return (f"{cut.total:,} row{'' if cut.total == 1 else 's'} the run took out "
                f"here. What they did differently is upstream of this stage.")
    return (f"{cut.total:,} row{'' if cut.total == 1 else 's'} still in the frame, "
            f"merged into a row this figure did not come through.")


def refuse_reason(error: UnresolvableFigure) -> str:
    return str(error)


def _read_run(project_id: str, run_id: str) -> WorkflowRunBranches:
    manifest = run_service.read_run_status(project_id, run_id)
    order = [record["stage_id"] for record in manifest["stage_records"]]
    rows = {record["stage_id"]: record["output_row_count"]
            for record in manifest["stage_records"]}
    stages = load_version_stages(project_id,
                                 run_service.read_pinned_version(project_id, run_id))
    workflow = Workflow(stages=stages)
    placed = {stage.id: workflow.find_workflow_stage(stage.id) for stage in stages}
    return reconstruct_run_branches(resolve_run_dir(project_id, run_id), placed,
                                    order, rows)
