"""Everything one run holds about a cited cell, and the single review its reviewers leave."""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import combinations

from app.core.figure_text import render_figure
from app.core.file_shape import VALUES_KEPT, measure_column_shape
from app.core.frames import convert_cell_to_json_value
from app.core.ids import ID
from app.core.json_types import JsonScalar
from app.models.branch_analysis import BranchReason
from app.models.citations import (
    ChallengeCitation,
    PublishedCitation,
    StageCitation,
    StageOutputCellCitation,
    StageOutputColumnCitation,
)
from app.models.claim_review import (
    BranchEvidenceItem,
    CitedShape,
    EvidenceBundle,
    InputColumnEvidenceItem,
    OutputEvidenceItem,
    StageEvidenceItem,
    find_claim_part_spans,
)
from app.models.records.claim_review import Challenge, ChallengeKind, ClaimPart, ClaimReview
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


def load_claim_review(claim_id: ID) -> ClaimReview | None:
    held = ClaimReview.find(claim_id=claim_id)
    if len(held) > 1:
        raise ClaimReviewRefused(
            [f"claim {claim_id} holds {len(held)} reviews; a review is written once"])
    return held[0] if held else None


def store_claim_review(project_id: ID, claim_id: ID, *, claim_parts: list[ClaimPart],
                       challenges: list[Challenge], summary: str, session_ids: list[ID],
                       corpus: str) -> ClaimReview:
    claim = claims_service.load_claim(project_id, claim_id)
    cited = _require_cell_citation(claim.citation)
    if load_claim_review(claim_id) is not None:
        raise ClaimReviewRefused(
            [f"claim {claim_id} already has a review; a re-review is a new claim"])
    issues = [*find_claim_part_issues(claim_parts, claim.text),
              *find_unprinted_evidence(challenges, corpus),
              *find_challenge_issues(challenges, len(claim_parts)),
              *find_citation_issues(project_id, cited.run_id, challenges)]
    if issues:
        raise ClaimReviewRefused(issues)
    review = ClaimReview(
        claim_id=claim_id, claim_parts=claim_parts, challenges=challenges, summary=summary,
        session_ids=session_ids)
    review.save()
    return review


def find_claim_part_issues(claim_parts: list[ClaimPart], text: str) -> list[str]:
    spans = find_claim_part_spans(text, claim_parts)
    missing = [
        f"claim part {index} {part.phrase!r}: the claim holds it fewer than "
        f"{part.occurrence} times"
        for index, (part, span) in enumerate(zip(claim_parts, spans)) if span is None
    ]
    return missing + _find_overlapping_claim_parts(spans)


def find_unprinted_evidence(challenges: list[Challenge], corpus: str) -> list[str]:
    return [
        f"{_name_challenge(index, challenge)}: evidence {challenge.evidence!r} is on no line "
        "of the pool"
        for index, challenge in enumerate(challenges)
        if not _read_whether_the_corpus_spells(corpus, challenge.evidence)
    ]


def find_challenge_issues(challenges: list[Challenge], claim_part_count: int) -> list[str]:
    return [
        f"{_name_challenge(index, challenge)}: claim_part_index {challenge.claim_part_index} "
        f"names no claim part; the claim has {claim_part_count}"
        for index, challenge in enumerate(challenges)
        if challenge.claim_part_index is not None
        and challenge.claim_part_index >= claim_part_count
    ]


def find_citation_issues(project_id: ID, run_id: ID, challenges: list[Challenge]) -> list[str]:
    cited = [(index, challenge, citation) for index, challenge in enumerate(challenges)
             for citation in challenge.citations]
    if not cited:
        return []
    held = _read_run_holdings(project_id, run_id, [citation for _, _, citation in cited])
    return [
        f"{_name_challenge(index, challenge)}: {citation.kind} citation {problem}"
        for index, challenge, citation in cited
        if (problem := _find_citation_problem(held, citation)) is not None
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
            rows_count=run_branches.row_count_per_branch_id[option.id])
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


def _read_whether_the_corpus_spells(corpus: str, evidence: str) -> bool:
    """At token boundaries: `220` inside `2200` is not printed."""
    if not evidence:
        return False
    pattern = rf"(?<!\w)(?<!\d[,.]){re.escape(evidence)}(?![,.]\d)(?!\w)"
    return re.search(pattern, corpus) is not None


def _require_cell_citation(citation: PublishedCitation) -> StageOutputCellCitation:
    if not isinstance(citation, StageOutputCellCitation):
        raise ClaimReviewRefused(
            ["a table claim has no sentence to review; only a cell claim is reviewed"])
    return citation


# ── what a challenge names ──


@dataclass(frozen=True)
class _StageOutput:
    row_count: int
    columns: frozenset[str]
    # Only the cited columns, each cell as JSON reads it.
    cells_by_column: dict[str, list[JsonScalar]]


@dataclass(frozen=True)
class _RunHoldings:
    run_id: ID
    # Only the cited stages that wrote an output in the run.
    outputs_by_stage_id: dict[str, _StageOutput]
    stage_ids: set[str]
    term_names: set[str]


def _name_challenge(index: int, challenge: Challenge) -> str:
    return f"challenge {index} ({ChallengeKind(challenge.kind).value})"


def _find_overlapping_claim_parts(spans: list[tuple[int, int] | None]) -> list[str]:
    resolved = [(index, span) for index, span in enumerate(spans) if span is not None]
    return [
        f"claim parts {first} and {second} overlap"
        for (first, (first_start, first_end)), (second, (second_start, second_end))
        in combinations(resolved, 2)
        if first_start < second_end and second_start < first_end
    ]


def _read_run_holdings(project_id: ID, run_id: ID,
                       citations: list[ChallengeCitation]) -> _RunHoldings:
    manifest = run_service.read_run_manifest(project_id, run_id)
    written = {record.stage_id for record in manifest.stage_records if record.output_path}
    wanted = _find_cited_columns_by_stage_id(run_id, citations)
    terms = terms_service.load_terms(project_id)
    return _RunHoldings(
        run_id=run_id,
        outputs_by_stage_id={
            stage_id: _read_stage_output_cells(project_id, run_id, stage_id, columns)
            for stage_id, columns in wanted.items() if stage_id in written},
        stage_ids={placed.id for placed in _read_workflow_stages(project_id, run_id)},
        term_names={noun.name for noun in terms.nouns.schemas}
        | {verb.name for verb in terms.verbs},
    )


def _find_cited_columns_by_stage_id(run_id: ID,
                                    citations: list[ChallengeCitation]) -> dict[str, set[str]]:
    wanted: dict[str, set[str]] = {}
    for citation in citations:
        in_this_run = not isinstance(citation, StageOutputCellCitation) or citation.run_id == run_id
        if isinstance(citation, (StageOutputCellCitation, StageOutputColumnCitation)) and in_this_run:
            wanted.setdefault(citation.stage_id, set()).add(citation.column)
    return wanted


def _read_stage_output_cells(project_id: ID, run_id: ID, stage_id: str,
                             columns: set[str]) -> _StageOutput:
    frame = run_service.read_stage_output(project_id, run_id, stage_id)
    held = {str(name) for name in frame.columns}
    return _StageOutput(
        row_count=len(frame), columns=frozenset(held),
        cells_by_column={
            name: [convert_cell_to_json_value(cell) for cell in frame[name].tolist()]
            for name in columns & held})


def _find_citation_problem(held: _RunHoldings, citation: ChallengeCitation) -> str | None:
    if isinstance(citation, StageOutputCellCitation):
        return _find_cell_problem(held, citation)
    if isinstance(citation, StageOutputColumnCitation):
        return _find_column_problem(held, citation.stage_id, citation.column)
    if isinstance(citation, StageCitation):
        if citation.stage_id in held.stage_ids:
            return None
        return f"names stage {citation.stage_id!r}, which the run's workflow does not hold"
    if citation.name in held.term_names:
        return None
    return f"names {citation.name!r}, which is no noun or verb in the project's terms"


def _find_cell_problem(held: _RunHoldings, citation: StageOutputCellCitation) -> str | None:
    if citation.run_id != held.run_id:
        return f"names run {citation.run_id!r}, not the claim's run {held.run_id!r}"
    column_problem = _find_column_problem(held, citation.stage_id, citation.column)
    if column_problem is not None:
        return column_problem
    output = held.outputs_by_stage_id[citation.stage_id]
    if not 0 <= citation.row_ordinal < output.row_count:
        return (f"names row {citation.row_ordinal}, which the output of "
                f"{citation.stage_id!r} does not hold ({output.row_count} rows)")
    # Rendered, both sides: 2200 and "2200" are one figure; 2200 and "2,200" are not.
    cell = render_figure(output.cells_by_column[citation.column][citation.row_ordinal])
    if cell != render_figure(citation.value):
        return f"gives value {citation.value!r}, but that cell holds {cell!r}"
    return None


def _find_column_problem(held: _RunHoldings, stage_id: str, column: str) -> str | None:
    output = held.outputs_by_stage_id.get(stage_id)
    if output is None:
        return f"names stage {stage_id!r}, which wrote no output in run {held.run_id!r}"
    if column not in output.columns:
        return f"names column {column!r}, which the output of {stage_id!r} does not hold"
    return None
