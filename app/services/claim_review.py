"""Everything one run holds about a cited cell, and the single review its attack leaves."""
from __future__ import annotations

import re

from app.core.figure_text import render_figure
from app.core.file_shape import VALUES_KEPT, measure_column_shape
from app.core.ids import ID
from app.models.branch_analysis import BranchReason
from app.models.claim_review import (
    BranchEvidenceItem,
    Challenge,
    CitedShape,
    EvidenceBundle,
    Grounding,
    InputColumnEvidenceItem,
    OutputEvidenceItem,
    Rewrite,
    StageEvidenceItem,
)
from app.models.claims import PublishedCitation, StageOutputCellCitation
from app.models.records.claim_review import ClaimReview
from app.models.stage import StageType
from app.models.terms import render_terms
from app.models.workflow import Workflow, find_stages_upstream_of, sort_stages_by_dependency
from app.models.workflow_stage import WorkflowStage
from app.services import claim_shapes
from app.services import claims as claims_service
from app.services import run as run_service
from app.services import scope as scope_service
from app.services import terms as terms_service
from app.services.errors import ClaimReviewRefused
from app.services.methodology import read_methodology
from app.services.versioning import load_version_stages


def build_evidence_bundle(project_id: ID, claim_id: ID) -> EvidenceBundle:
    claim = claims_service.load_claim(project_id, claim_id)
    cited = _require_cell_citation(claim.citation)
    run_id = cited.run_id
    manifest = run_service.read_run_manifest(project_id, run_id)
    stages = _read_workflow_stages(project_id, run_id)
    written = {record.stage_id for record in manifest.stage_records if record.output_path}
    return EvidenceBundle(
        project_id=project_id, run_id=run_id, claim_id=claim.id, claim_text=claim.text,
        claim_context=claim.context, cited=cited,
        shape=_read_shape(project_id, claim.shape_id),
        run_read_everything=claims_service.read_whether_the_run_read_everything(manifest),
        outputs=_read_outputs(run_id, claims_service.find_output_of_claim(claim).slug),
        stages=_read_stages(stages, cited.stage_id),
        branches=_read_branches(project_id, run_id),
        input_columns=_read_input_columns(project_id, run_id, stages, written),
        terms=render_terms(terms_service.load_terms(project_id)),
        methodology=read_methodology(project_id),
    )


def load_claim_review(project_id: ID, claim_id: ID) -> ClaimReview | None:
    held = [review for review in ClaimReview.find(claim_id=claim_id)
            if review.project_id == project_id]
    if len(held) > 1:
        raise ClaimReviewRefused(
            [f"claim {claim_id} holds {len(held)} reviews; a review is written once"])
    return held[0] if held else None


def store_claim_review(project_id: ID, claim_id: ID, *, grounding: list[Grounding],
                       challenges: list[Challenge], rewrites: list[Rewrite], summary: str,
                       session_ids: list[ID], corpus: str) -> ClaimReview:
    claim = claims_service.load_claim(project_id, claim_id)
    _require_cell_citation(claim.citation)
    if load_claim_review(project_id, claim_id) is not None:
        raise ClaimReviewRefused(
            [f"claim {claim_id} already has a review; a re-attack is a new claim"])
    issues = (find_grounding_issues(grounding, claim.text)
              + find_unbacked_challenges(challenges, corpus)
              + find_challenge_issues(challenges, len(grounding)))
    if issues:
        raise ClaimReviewRefused(issues)
    review = ClaimReview(
        project_id=project_id, claim_id=claim_id, run_id=claim.citation.run_id,
        grounding=grounding, challenges=challenges, proposed_rewrites=rewrites,
        summary=summary, session_ids=session_ids)
    review.save()
    return review


def find_unbacked_challenges(challenges: list[Challenge], corpus: str) -> list[str]:
    return [
        f"challenge {index} ({challenge.kind}): backing {challenge.backing!r} is in "
        "neither the pool nor an attacker's evidence"
        for index, challenge in enumerate(challenges)
        if not _read_whether_the_corpus_spells(corpus, challenge.backing)
    ]


def find_challenge_issues(challenges: list[Challenge], grounding_count: int) -> list[str]:
    return [
        f"challenge {index} ({challenge.kind}): grounding_index "
        f"{challenge.grounding_index} names no phrase; the review grounds "
        f"{grounding_count}"
        for index, challenge in enumerate(challenges)
        if challenge.grounding_index is not None
        and challenge.grounding_index >= grounding_count
    ]


def find_grounding_issues(grounding: list[Grounding], text: str) -> list[str]:
    return [
        *(f"grounding {index} ends at {phrase.end}, past the end of a claim {len(text)} "
          "characters long" for index, phrase in enumerate(grounding)
          if phrase.end > len(text)),
        *(f"grounding {index} starts at {phrase.start}, which is not before its end "
          f"{phrase.end}" for index, phrase in enumerate(grounding)
          if phrase.start >= phrase.end),
        *_find_overlapping_spans(grounding),
    ]


# ── what the run holds ──


def _read_workflow_stages(project_id: ID, run_id: ID) -> list[WorkflowStage]:
    version_id = run_service.read_pinned_version(project_id, run_id)
    workflow = Workflow(stages=load_version_stages(project_id, version_id))
    ordered = sort_stages_by_dependency(workflow.stages)
    return [workflow.find_workflow_stage(stage.id) for stage in ordered]


def _read_shape(project_id: ID, shape_id: ID | None) -> CitedShape:
    shape = claim_shapes.load_claim_shape(project_id, shape_id) if shape_id else None
    if shape is None:
        raise ClaimReviewRefused([f"this project holds no claim shape '{shape_id}'"])
    return CitedShape(
        label=shape.label, universe=shape.universe, importance=shape.importance,
        qualifiers=list(shape.qualifiers),
        context_columns=[column.name for column in shape.context])


def _read_outputs(run_id: ID, cited_slug: str) -> list[OutputEvidenceItem]:
    return [
        OutputEvidenceItem(
            slug=output.slug, label=output.label, primary=output.primary,
            stage_id=output.citation.stage_id, value=_read_output_value(output.citation),
            cited=output.slug == cited_slug)
        for output in claims_service.read_every_run_output(run_id)
    ]


def _read_output_value(citation: PublishedCitation) -> str:
    """A table names rows, not one cell; its row count is the fact it carries."""
    if isinstance(citation, StageOutputCellCitation):
        return render_figure(citation.value)
    return f"{citation.rectangle.count_rows():,} rows"


def _read_stages(stages: list[WorkflowStage], cited_stage_id: ID) -> list[StageEvidenceItem]:
    feeding = find_stages_upstream_of([placed.stage for placed in stages], cited_stage_id)
    return [
        StageEvidenceItem(
            stage_id=placed.id, type=placed.stage.type,
            description=placed.stage.description,
            input_ids=[read.id for read in placed.inputs], code=_read_stage_code(placed),
            feeds_the_cited_stage=placed.id in feeding)
        for placed in stages
    ]


def _read_branches(project_id: ID, run_id: ID) -> list[BranchEvidenceItem]:
    run_branches = scope_service.read_run_branches(project_id, run_id)
    return [
        BranchEvidenceItem(
            branch_id=option.id, stage_id=option.stage_id, reason=option.reason.value,
            role=option.role.value, label=option.label, source_code=option.source_code,
            rows_count=run_branches.row_count_per_branch_id.get(option.id))
        for option in run_branches.branch_options.values()
        if option.reason is not BranchReason.merge
    ]


def _read_input_columns(project_id: ID, run_id: ID, stages: list[WorkflowStage],
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


def _read_whether_the_corpus_spells(corpus: str, backing: str) -> bool:
    """At token boundaries: `220` inside `2200` backs nothing."""
    if not backing:
        return False
    return re.search(rf"(?<![\w,.]){re.escape(backing)}(?![\w,.])", corpus) is not None


def _require_cell_citation(citation: PublishedCitation) -> StageOutputCellCitation:
    if not isinstance(citation, StageOutputCellCitation):
        raise ClaimReviewRefused(
            ["a table claim has no sentence to attack; only a cell claim is attacked"])
    return citation


def _find_overlapping_spans(grounding: list[Grounding]) -> list[str]:
    issues, reached = [], 0
    for phrase in sorted(grounding, key=lambda phrase: phrase.start):
        if phrase.start < reached:
            issues.append(f"the phrase at {phrase.start}-{phrase.end} overlaps the one "
                          "that ends after it starts")
        reached = max(reached, phrase.end)
    return issues
