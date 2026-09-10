"""What a run holds, read piece by piece for a claim's evidence bundle."""
from __future__ import annotations

from app.core.figure_text import render_figure
from app.core.file_shape import VALUES_KEPT, measure_column_shape
from app.core.ids import ID
from app.models.branch_analysis import BranchReason
from app.models.claim_review import (
    BranchEvidenceItem,
    InputColumnEvidenceItem,
    OutputEvidenceItem,
    StageEvidenceItem,
)
from app.models.claims import PublishedCitation, StageOutputCellCitation
from app.models.stage import StageType
from app.models.workflow import find_stages_upstream_of
from app.models.workflow_stage import WorkflowStage
from app.services import claims as claims_service
from app.services import run as run_service
from app.services import scope as scope_service


def read_outputs(run_id: ID, cited_slug: str) -> list[OutputEvidenceItem]:
    return [
        OutputEvidenceItem(
            slug=output.slug, label=output.label, primary=output.primary,
            stage_id=output.citation.stage_id, value=read_output_value(output.citation),
            cited=output.slug == cited_slug)
        for output in claims_service.read_every_run_output(run_id)
    ]


def read_output_value(citation: PublishedCitation) -> str:
    """A table names rows, not one cell; its row count is the fact it carries."""
    if isinstance(citation, StageOutputCellCitation):
        return render_figure(citation.value)
    return f"{citation.rectangle.count_rows():,} rows"


def read_stages(stages: list[WorkflowStage], cited_stage_id: ID) -> list[StageEvidenceItem]:
    feeding = find_stages_upstream_of([placed.stage for placed in stages], cited_stage_id)
    return [
        StageEvidenceItem(
            stage_id=placed.id, type=placed.stage.type,
            description=placed.stage.description,
            input_ids=[read.id for read in placed.inputs], code=_read_stage_code(placed),
            feeds_the_cited_stage=placed.id in feeding)
        for placed in stages
    ]


def read_branches(project_id: ID, run_id: ID) -> list[BranchEvidenceItem]:
    run_branches = scope_service.read_run_branches(project_id, run_id)
    return [
        BranchEvidenceItem(
            branch_id=option.id, stage_id=option.stage_id, reason=option.reason.value,
            role=option.role.value, label=option.label, source_code=option.source_code,
            rows_count=run_branches.row_count_per_branch_id[option.id])
        for option in run_branches.branch_options.values()
        if option.reason is not BranchReason.merge
    ]


def read_input_columns(project_id: ID, run_id: ID, stages: list[WorkflowStage],
                       written: set[str]) -> list[InputColumnEvidenceItem]:
    measured: list[InputColumnEvidenceItem] = []
    for placed in stages:
        if placed.stage.type == StageType.input_data and placed.id in written:
            measured.extend(_measure_stage_columns(project_id, run_id, placed.id))
    return measured


def _measure_stage_columns(project_id: ID, run_id: ID,
                           stage_id: ID) -> list[InputColumnEvidenceItem]:
    frame = run_service.read_stage_output(project_id, run_id, stage_id)
    row_count = len(frame)
    measured = []
    for name in frame.columns:
        present = [str(value) for value in frame[name].dropna().tolist()]
        shape = measure_column_shape(str(name), present, null_count=row_count - len(present),
                                     max_values=VALUES_KEPT)
        measured.append(InputColumnEvidenceItem(
            stage_id=stage_id, column=shape.column, kind=shape.kind.value,
            row_count=row_count, filled_count=shape.filled_count,
            null_count=shape.null_count, blank_count=shape.blank_count,
            distinct_count=shape.distinct_count, top=shape.top))
    return measured


def _read_stage_code(placed: WorkflowStage) -> str:
    block = placed.stage.find_authored_code_block()
    return str(block.code) if block is not None else ""
