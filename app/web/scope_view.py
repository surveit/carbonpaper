"""The analysis states the facts; every sentence a reader sees is written here."""

from __future__ import annotations

from app.web.figure_text import render_figure
from app.core.errors import StageNotInRun
from app.models.branch_analysis import BranchId, BranchRole
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.models.workflow import Workflow
from app.core.run_status import is_run_still_going
from app.runtime.branch_analysis import (
    WorkflowRunBranches,
    load_run_branches,
    reconstruct_run_branches,
)
from app.services import run as run_service
from app.services.scope import find_rows_reached_per_stage
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.loading import MAX_TABLE_ROWS, load_manifest, load_output_rows_at
from app.web.run_stage_panel import resolve_panel_links
from app.web.stage_diff import build_stage_diff
from app.web.scope_payload import (
    CutRows,
    ScopeMap,
    build_scope_map,
    find_cuts_to_offer,
)


def load_scope_map(project_id: str, run_id: str, citation: StageOutputCellCitation,
                   expand: frozenset[StageId] = frozenset()
                   ) -> tuple[ScopeMap, dict[BranchId, CutRows]]:
    run_branches = read_run_branches(project_id, run_id)
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    scope = build_scope_map(run_branches, project_id, run_id, outputs, citation,
                            expand)
    return scope, find_cuts_to_offer(run_branches, outputs, scope, expand)


def load_the_rows_that_reached(project_id: str, run_id: str, stage_id: StageId,
                               cited_row: int, at: StageId) -> dict[str, object]:
    """What the run page shows for `at`, cut to the rows the cited figure came through."""
    run_branches = read_run_branches(project_id, run_id)
    reached = find_rows_reached_per_stage(run_branches, [(stage_id, cited_row)])
    manifest = load_manifest(project_id, run_id)
    record = next((entry for entry in manifest.get("stage_records", [])
                   if entry.get("stage_id") == at), None)
    if record is None:
        raise StageNotInRun(f"no stage '{at}' in this run")
    pinned = run_service.load_pinned_stage_def(project_id, manifest, at)
    at_rows = sorted(reached.get(at, ()))
    return {
        "project": project_id, "run_id": run_id, "stage": record,
        "diff": build_stage_diff(
            pinned.workflow_stage, resolve_run_dir(project_id, run_id),
            record.get("output_path"),
            {entry.get("stage_id"): entry.get("output_path")
             for entry in manifest.get("stage_records", [])},
            at_rows=at_rows),
        "links": resolve_panel_links(project_id, run_id),
        "ordinals": at_rows,
        "preview": load_output_rows_at(
            resolve_run_dir(project_id, run_id), record.get("output_path"),
            at_rows, MAX_TABLE_ROWS),
        "full_rows": True,
    }


def say_what_no_row_fed(scope: ScopeMap) -> str | None:
    """A row the run recorded nothing behind still counts as a row, so it is said here."""
    unfed = len(scope.covers.fed_by_no_rows)
    if not unfed:
        return None
    named = len(scope.covers.ordinals)
    if unfed == named:
        return (f"No row fed this figure: the run recorded nothing behind the "
                f"{render_figure(unfed)} row{'' if unfed == 1 else 's'} it names at "
                f"{scope.covers.at_stage}.")
    return (f"The run recorded nothing behind {render_figure(unfed)} of the {render_figure(named)} rows this "
            f"figure names at {scope.covers.at_stage}.")


def say_why_rows_left(cut: CutRows, role: BranchRole) -> str:
    if role is BranchRole.removes:
        return (f"{render_figure(cut.total)} row{'' if cut.total == 1 else 's'} the run took out "
                f"here. What they did differently is upstream of this stage.")
    return (f"{render_figure(cut.total)} row{'' if cut.total == 1 else 's'} still in the frame, "
            f"merged into a row this figure did not come through.")


def read_run_branches(project_id: str, run_id: str) -> WorkflowRunBranches:
    manifest = run_service.read_run_status(project_id, run_id)
    # An interrupted run leaves records for stages it never reached: no frame, none owed.
    records_with_a_frame = [record for record in manifest["stage_records"]
                            if record.get("output_path")]
    order = [record["stage_id"] for record in records_with_a_frame]
    rows = {record["stage_id"]: record["output_row_count"]
            for record in records_with_a_frame}
    pinned_version_id = run_service.read_pinned_version(project_id, run_id)
    stages = load_version_stages(project_id, pinned_version_id)
    workflow = Workflow(stages=stages)
    placed = {stage.id: workflow.find_workflow_stage(stage.id)
              for stage in stages if stage.id in rows}
    run_dir = resolve_run_dir(project_id, run_id)
    if is_run_still_going(manifest["status"]):
        return reconstruct_run_branches(run_dir, placed, order, rows)
    return load_run_branches(run_dir, placed, order, rows, pinned_version_id)
