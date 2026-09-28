"""Everything one run holds about a cited cell, and the single review its reviewers leave."""
from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa

from app.core.figure_text import render_figure
from app.core.file_shape import VALUES_KEPT, measure_column_shape
from app.core.frames import list_table_rows, read_native_cell_as_json
from app.core.ids import ID
from app.core.json_types import JsonScalar
from app.models.branch_analysis import BranchReason
from app.models.citations import (
    address_citation,
    CellCitation,
    ChallengeCitation,
    PublishedCitation,
    SourceSpanCitation,
    StageCitation,
    StageOutputCellCitation,
    StageOutputColumnCitation,
)
from app.models.claim_review import (
    BranchEvidenceItem,
    CitedPassage,
    EvidenceBundle,
    InputColumnEvidenceItem,
    StageEvidenceItem,
    find_claim_part_spans,
)
from app.models.locators import label_locator

from app.models.records.claim_review import (
    Challenge,
    ChallengeKind,
    ClaimReview,
    DraftChallenge,
)
from app.models.records.workflow_version import WorkflowVersion
from app.models.stage import StageType
from app.models.workflow import Workflow, find_stages_upstream_of, sort_stages_by_dependency
from app.models.workflow_stage import WorkflowStage
from app.services import claims as claims_service
from app.models.run_manifest import RunKind
from app.services import run as run_service
from app.services import scope as scope_service
from app.services.errors import ClaimReviewRefused
from app.services.versioning import load_version

# Either side of a cited quote: about a sentence each way.
_PASSAGE_CONTEXT_CHARACTERS = 100


def build_evidence_bundle(project_id: ID, claim_id: ID) -> EvidenceBundle:
    claim = claims_service.load_claim(project_id, claim_id)
    cited = _require_cell_citation(claim.citation)
    run_id = cited.run_id
    manifest = run_service.read_run_manifest(project_id, run_id, RunKind.production)
    version = _load_pinned_version(project_id, run_id)
    stages = _read_workflow_stages(version)
    written = {record.stage_id for record in manifest.stage_records if record.output_path}
    return EvidenceBundle(
        claim_id=claim.id, claim_text=claim.text, claim_context=claim.context, cited=cited,
        cited_passage=_read_cited_passage(project_id, cited),
        shape=claims_service.load_required_claim_shape(project_id, claim.shape_id).read_input(),
        run_read_everything=claims_service.read_whether_the_run_read_everything(manifest),
        outputs=claims_service.read_every_run_output(run_id),
        cited_slug=claims_service.find_output_of_claim(claim).slug,
        stages=_read_stages(stages, cited.stage_id),
        branches=_read_branches(project_id, run_id),
        input_columns=_read_input_columns(project_id, run_id, stages, written),
        method=version.method,
    )


def load_claim_review(claim_id: ID) -> ClaimReview | None:
    held = ClaimReview.find(claim_id=claim_id)
    if len(held) > 1:
        raise ClaimReviewRefused(
            [f"claim {claim_id} holds {len(held)} reviews; a review is written once"])
    return held[0] if held else None


def store_claim_review(project_id: ID, claim_id: ID, *, challenges: list[DraftChallenge],
                       session_id: ID) -> ClaimReview:
    claim = claims_service.load_claim(project_id, claim_id)
    cited = _require_cell_citation(claim.citation)
    if load_claim_review(claim_id) is not None:
        raise ClaimReviewRefused(
            [f"claim {claim_id} already has a review; a re-review is a new claim"])
    addressed = [_address_challenge(project_id, one) for one in challenges]
    issues = [*find_challenge_issues(addressed, claim.text),
              *find_citation_issues(project_id, cited.run_id, addressed)]
    if issues:
        raise ClaimReviewRefused(issues)
    review = ClaimReview(claim_id=claim_id, challenges=addressed, session_id=session_id)
    review.save()
    return review


def _address_challenge(project_id: ID, challenge: DraftChallenge) -> Challenge:
    return Challenge.model_validate({
        **challenge.model_dump(exclude={"citations"}),
        "citations": [address_citation(project_id, one) for one in challenge.citations],
    })


def find_challenge_issues(challenges: list[Challenge], text: str) -> list[str]:
    landed = [(index, one, one.claim_part)
              for index, one in enumerate(challenges) if one.claim_part is not None]
    spans = find_claim_part_spans(text, [part for _, _, part in landed])
    return [
        f"{_name_challenge(index, one)}: the claim holds {part.phrase!r} fewer than "
        f"{part.occurrence} times"
        for (index, one, part), span in zip(landed, spans) if span is None
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


def _read_cited_passage(project_id: ID, cited: CellCitation) -> CitedPassage | None:
    if not isinstance(cited, SourceSpanCitation):
        return None
    span = cited.build_span()
    try:
        page_text = run_service.verify_run_span(project_id, cited.run_id, span)
    except run_service.SPAN_REFUSALS as refusal:
        raise ClaimReviewRefused(
            [f"the claim's quote does not hold in its file: {refusal}"]) from refusal
    locator = cited.locator
    return CitedPassage(
        quote=cited.quote, locator_label=label_locator(locator),
        before=page_text[max(0, locator.start - _PASSAGE_CONTEXT_CHARACTERS):locator.start],
        after=page_text[locator.end:locator.end + _PASSAGE_CONTEXT_CHARACTERS])


def _load_pinned_version(project_id: ID, run_id: ID) -> WorkflowVersion:
    return load_version(project_id, run_service.read_pinned_version(project_id, run_id))


def _read_workflow_stages(version: WorkflowVersion) -> list[WorkflowStage]:
    workflow = Workflow(stages=version.stages)
    ordered = sort_stages_by_dependency(workflow.stages)
    return [workflow.find_workflow_stage(stage.id) for stage in ordered]


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
            label=option.label, source_code=option.source_code,
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
        measured.append(InputColumnEvidenceItem(stage_id=stage_id, row_count=row_count,
                                                shape=shape))
    return measured


def _read_stage_code(placed: WorkflowStage) -> str:
    block = placed.stage.find_authored_code_block()
    return str(block.code) if block is not None else ""


def _require_cell_citation(citation: PublishedCitation) -> CellCitation:
    if not isinstance(citation, CellCitation):
        raise ClaimReviewRefused(
            ["a table claim has no sentence to review; only a cell claim is reviewed"])
    return citation


# ── what a challenge names ──


@dataclass(frozen=True)
class _RunHoldings:
    project_id: ID
    run_id: ID
    # Only the cited stages that wrote an output in the run, Arrow-native as the run wrote them.
    outputs_by_stage_id: dict[str, pa.Table]
    stage_ids: set[str]
    term_names: set[str]


def _name_challenge(index: int, challenge: Challenge) -> str:
    return f"challenge {index} ({ChallengeKind(challenge.kind).value})"


def _read_run_holdings(project_id: ID, run_id: ID,
                       citations: list[ChallengeCitation]) -> _RunHoldings:
    manifest = run_service.read_run_manifest(project_id, run_id, RunKind.production)
    written = {record.stage_id for record in manifest.stage_records if record.output_path}
    version = _load_pinned_version(project_id, run_id)
    return _RunHoldings(
        project_id=project_id,
        run_id=run_id,
        outputs_by_stage_id={
            stage_id: run_service.read_stage_output_table(project_id, run_id, stage_id)
            for stage_id in _find_cited_stage_ids(run_id, citations) if stage_id in written},
        stage_ids={placed.id for placed in _read_workflow_stages(version)},
        term_names=_list_term_names(version),
    )


def _list_term_names(version: WorkflowVersion) -> set[str]:
    tables = {table["name"] for table in version.schemas}
    if version.method is None:
        return tables
    return (tables | {row_type.id for row_type in version.method.row_types}
            | {verb.name for verb in version.method.verbs})


def _find_cited_stage_ids(run_id: ID, citations: list[ChallengeCitation]) -> set[str]:
    # A cell of another run is refused by its run id, so its stage is never read.
    return {
        citation.stage_id for citation in citations
        if isinstance(citation, StageOutputColumnCitation)
        or (isinstance(citation, CellCitation) and citation.run_id == run_id)}


def _find_citation_problem(held: _RunHoldings, citation: ChallengeCitation) -> str | None:
    if isinstance(citation, StageOutputCellCitation):
        return _find_cell_problem(held, citation)
    if isinstance(citation, SourceSpanCitation):
        return _find_span_problem(held, citation)
    if isinstance(citation, StageOutputColumnCitation):
        return _find_column_problem(held, citation.stage_id, citation.column)
    if isinstance(citation, StageCitation):
        if citation.stage_id in held.stage_ids:
            return None
        return f"names stage {citation.stage_id!r}, which the run's workflow does not hold"
    if citation.name in held.term_names:
        return None
    return f"names {citation.name!r}, which the run's workflow version does not define"


def _find_cell_problem(held: _RunHoldings, citation: StageOutputCellCitation) -> str | None:
    row_problem = _find_row_problem(held, citation)
    if row_problem is not None:
        return row_problem
    table = held.outputs_by_stage_id[citation.stage_id]
    cell = read_native_cell_as_json(table, citation.column, citation.row_ordinal)
    if not _is_the_cell_value(cell, citation.value):
        return f"gives value {citation.value!r}, but that cell holds {render_figure(cell)!r}"
    return None


def _find_span_problem(held: _RunHoldings, citation: SourceSpanCitation) -> str | None:
    row_problem = _find_row_problem(held, citation)
    if row_problem is not None:
        return row_problem
    try:
        run_service.verify_run_span(held.project_id, held.run_id, citation.build_span())
    except run_service.SPAN_REFUSALS as refusal:
        return str(refusal)
    table = held.outputs_by_stage_id[citation.stage_id]
    if not citation.is_held_in(_read_cell_as_python(table, citation.column, citation.row_ordinal)):
        return (f"quotes {citation.quote!r} on {label_locator(citation.locator)}, "
                "which that cell does not hold")
    return None


def _find_row_problem(held: _RunHoldings, citation: CellCitation) -> str | None:
    if citation.run_id != held.run_id:
        return f"names run {citation.run_id!r}, not the claim's run {held.run_id!r}"
    column_problem = _find_column_problem(held, citation.stage_id, citation.column)
    if column_problem is not None:
        return column_problem
    row_count = held.outputs_by_stage_id[citation.stage_id].num_rows
    if not 0 <= citation.row_ordinal < row_count:
        return (f"names row {citation.row_ordinal}, which the output of "
                f"{citation.stage_id!r} does not hold ({row_count} rows)")
    return None


def _read_cell_as_python(table: pa.Table, column: str, row_ordinal: int) -> object:
    # Not as JSON: a struct cell reads as its fields, which JSON conversion stringifies.
    return list_table_rows(table.select([column]).slice(row_ordinal, 1))[0][column]


def _is_the_cell_value(cell: JsonScalar, cited: JsonScalar) -> bool:
    """Rendered, so 22000 matches "22,000"; or numeric, so 62187729.0 matches 62187729."""
    if render_figure(cell) == render_figure(cited):
        return True
    held = _read_number(cell)
    return held is not None and held == _read_number(cited)


def _read_number(value: JsonScalar) -> float | None:
    # A bool is not a number here, so True never matches 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _find_column_problem(held: _RunHoldings, stage_id: str, column: str) -> str | None:
    table = held.outputs_by_stage_id.get(stage_id)
    if table is None:
        return f"names stage {stage_id!r}, which wrote no output in run {held.run_id!r}"
    if column not in table.column_names:
        return f"names column {column!r}, which the output of {stage_id!r} does not hold"
    return None
