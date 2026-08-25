"""The analysis states the facts; every sentence a reader sees is written here."""

from __future__ import annotations

from app.core.errors import StageNotInRun
from app.models.branch_analysis import BranchId, BranchRole, FrameScale, MergeGroup
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.models.workflow import Workflow
from app.runtime.branch_analysis import (
    WorkflowRunBranches,
    reconstruct_run_branches,
)
from app.services import run as run_service
from app.services.scope import (
    find_lookup_table_stages,
    find_rows_reached_per_stage,
)
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.merge_alias import (
    count_merge_groups,
    read_group_by,
    read_one_merge_group,
    resolve_merge_groups,
)
from app.web.scope_payload import (
    CutRows,
    read_cut,
    ScopeMap,
    build_scope_map,
    find_cuts_to_offer,
)


def load_scope_map(project_id: str, run_id: str, citation: StageOutputCellCitation
                   ) -> tuple[ScopeMap, dict[BranchId, CutRows], set[StageId]]:
    run_branches = read_run_branches(project_id, run_id)
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    scope = build_scope_map(run_branches, project_id, run_id, outputs, citation)
    cuts = find_cuts_to_offer(run_branches, outputs, scope)
    return scope, cuts, find_lookup_table_stages(run_branches)


def load_merge_groups(project_id: str, run_id: str,
                      citation: StageOutputCellCitation, merge: StageId,
                      offset: int) -> tuple[list[MergeGroup], int]:
    """De-alias one merge stage: a page of its groups, and how many there are."""
    run_branches = read_run_branches(project_id, run_id)
    if merge not in run_branches.stages:
        raise StageNotInRun(f"no stage '{merge}' in this run")
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    reached = find_rows_reached_per_stage(
        run_branches, [(citation.stage_id, citation.row_ordinal)])
    return (resolve_merge_groups(run_branches, outputs, merge,
                                 reached.get(merge, set()), offset),
            count_merge_groups(run_branches, merge))


def load_one_merge_group(project_id: str, run_id: str, merge: StageId, ordinal: int
                         ) -> tuple[CutRows | None, MergeGroup, list[str]]:
    """The rows behind one group, what it stands for, and the keys that name it."""
    run_branches = read_run_branches(project_id, run_id)
    if merge not in run_branches.stages:
        raise StageNotInRun(f"no stage '{merge}' in this run")
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    # Named before its rows are read: an ordinal the stage never grouped raises here.
    named = read_one_merge_group(run_branches, outputs, merge, ordinal)
    return (read_cut(run_branches, outputs, named.branch), named,
            read_group_by(run_branches, merge))


def say_what_the_rows_answer(scope: ScopeMap) -> str:
    """One sentence under the figure: which rows the drawing is about."""
    covered = len(scope.covers.ordinals)
    where = f"{covered:,} row{'' if covered == 1 else 's'} of {scope.covers.at_stage}"
    merged_at = scope.covers.regrained_at[1:]
    if not merged_at:
        return f"Computed from {where}."
    return (f"Computed from {where}, merged at {' and '.join(merged_at)} before this "
            f"figure was taken.")


def say_what_no_row_fed(scope: ScopeMap) -> str | None:
    """A row the run recorded nothing behind still counts as a row, so it is said here."""
    unfed = len(scope.covers.fed_by_no_rows)
    if not unfed:
        return None
    named = len(scope.covers.ordinals)
    if unfed == named:
        return (f"No row fed this figure: the run recorded nothing behind the "
                f"{unfed:,} row{'' if unfed == 1 else 's'} it names at "
                f"{scope.covers.at_stage}.")
    return (f"The run recorded nothing behind {unfed:,} of the {named:,} rows this "
            f"figure names at {scope.covers.at_stage}.")


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


def read_run_branches(project_id: str, run_id: str) -> WorkflowRunBranches:
    manifest = run_service.read_run_status(project_id, run_id)
    # An interrupted run leaves records for stages it never reached: no frame, none owed.
    records_with_a_frame = [record for record in manifest["stage_records"]
                            if record.get("output_path")]
    order = [record["stage_id"] for record in records_with_a_frame]
    rows = {record["stage_id"]: record["output_row_count"]
            for record in records_with_a_frame}
    stages = load_version_stages(project_id,
                                 run_service.read_pinned_version(project_id, run_id))
    workflow = Workflow(stages=stages)
    placed = {stage.id: workflow.find_workflow_stage(stage.id)
              for stage in stages if stage.id in rows}
    return reconstruct_run_branches(resolve_run_dir(project_id, run_id), placed,
                                    order, rows)
