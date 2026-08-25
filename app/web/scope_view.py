"""The analysis states the facts; every sentence a reader sees is written here."""

from __future__ import annotations

from app.models.branch_analysis import BranchId, BranchRole
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.models.workflow import Workflow
from app.runtime.branch_analysis import (
    WorkflowRunBranches,
    reconstruct_run_branches,
)
from app.services import run as run_service
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.scope_payload import (
    CutRows,
    ScopeMap,
    StageRows,
    build_scope_map,
    find_cuts_to_offer,
    read_stage_rows,
)


def load_scope_map(project_id: str, run_id: str, citation: StageOutputCellCitation,
                   expand: frozenset[StageId] = frozenset()
                   ) -> tuple[ScopeMap, dict[BranchId, CutRows]]:
    run_branches = read_run_branches(project_id, run_id)
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    scope = build_scope_map(run_branches, project_id, run_id, outputs, citation,
                            expand)
    return scope, find_cuts_to_offer(run_branches, outputs, scope)


def load_stage_rows(project_id: str, run_id: str, citation: StageOutputCellCitation,
                    at: StageId, held: frozenset[BranchId], behind: BranchId | None,
                    expand: frozenset[StageId] = frozenset()) -> StageRows:
    run_branches = read_run_branches(project_id, run_id)
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    scope = build_scope_map(run_branches, project_id, run_id, outputs, citation, expand)
    return read_stage_rows(run_branches, outputs, scope, at, held, behind)


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
