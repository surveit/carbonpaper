"""Everything one run holds about a cited cell, and the single review its attack leaves."""
from __future__ import annotations

import re

from app.compiler.claim_attack.evidence import render_evidence_pool
from app.compiler.claim_attack.run import PARENT_ROLE, start_claim_attack_agents
from app.core.agent.store import AgentSession
from app.core.ids import ID
from app.models.claim_review import (
    Challenge,
    CitedShape,
    ClaimAttackResult,
    EvidenceBundle,
    Grounding,
    Rewrite,
    find_grounding_issues,
    read_whether_a_backing_is_a_phrase,
)
from app.models.claims import ClaimStatus, PublishedCitation, StageOutputCellCitation
from app.models.records.claim_review import ClaimReview
from app.models.terms import render_terms
from app.models.workflow import Workflow, sort_stages_by_dependency
from app.models.workflow_stage import WorkflowStage
from app.services import claim_evidence
from app.services import claim_shapes
from app.services import claims as claims_service
from app.services import run as run_service
from app.services import terms as terms_service
from app.services.errors import ClaimReviewRefused
from app.services.methodology import read_methodology
from app.services.versioning import load_version_stages


def start_claim_attack(project_id: ID, claim_id: ID, *, model: str) -> str:
    """Must be called from the server event loop — the seven turns run as a task there."""
    claim = claims_service.load_claim(project_id, claim_id)
    _refuse_a_claim_already_reviewed(project_id, claim_id)
    _refuse_a_claim_already_under_attack(claim_id)
    if claim.status != ClaimStatus.submitted:
        raise ClaimReviewRefused(
            [f"claim {claim_id} is {claim.status}; only a submitted claim is attacked"])
    bundle = build_evidence_bundle(project_id, claim_id)
    return start_claim_attack_agents(
        bundle=bundle, model=model,
        on_answer=lambda result: _finish_claim_attack(project_id, claim_id, bundle, result),
    )


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
        outputs=claim_evidence.read_outputs(
            run_id, claims_service.find_output_of_claim(claim).slug),
        stages=claim_evidence.read_stages(stages, cited.stage_id),
        branches=claim_evidence.read_branches(project_id, run_id),
        input_columns=claim_evidence.read_input_columns(project_id, run_id, stages, written),
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
    _refuse_a_claim_already_reviewed(project_id, claim_id)
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
        f"challenge {index} ({challenge.kind}): backing {challenge.backing!r} "
        "is on no line of the pool"
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


def read_whether_the_pool_prints(pool: str, text: str) -> bool:
    """At token boundaries: `220` inside `2200` is not printed. A wrap is not a new phrase."""
    printed, phrase = re.sub(r"\s+", " ", pool), re.sub(r"\s+", " ", text)
    pattern = rf"(?<!\w)(?<!\d[,.]){re.escape(phrase)}(?![,.]\d)(?!\w)"
    return re.search(pattern, printed) is not None


def _finish_claim_attack(project_id: ID, claim_id: ID, bundle: EvidenceBundle,
                         result: ClaimAttackResult) -> None:
    """The pool alone is the corpus: neither the claim nor an attacker backs a challenge."""
    store_claim_review(
        project_id, claim_id,
        grounding=result.answers.grounding.phrases,
        challenges=result.draft.challenges,
        rewrites=result.answers.meaning.rewrites,
        summary=result.draft.summary,
        session_ids=result.session_ids,
        corpus=render_evidence_pool(bundle),
    )


def _refuse_a_claim_already_under_attack(claim_id: ID) -> None:
    """Two attacks would write two reviews of one claim, and the second is refused storage."""
    if _find_running_attacks(claim_id):
        raise ClaimReviewRefused(
            [f"an attack on claim {claim_id} is already running; it is attacked once at a time"])


def _find_running_attacks(claim_id: ID) -> list[AgentSession]:
    return [session for session in AgentSession.list()
            if session.context.get("role") == PARENT_ROLE
            and session.context.get("claim_id") == claim_id
            and session.active_turn is not None]


def _refuse_a_claim_already_reviewed(project_id: ID, claim_id: ID) -> None:
    if load_claim_review(project_id, claim_id) is not None:
        raise ClaimReviewRefused(
            [f"claim {claim_id} already has a review; a re-attack is a new claim"])


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


def _read_whether_the_corpus_spells(corpus: str, backing: str) -> bool:
    """A backing is a printed PHRASE: a lone digit the pool prints everywhere backs nothing."""
    return (read_whether_a_backing_is_a_phrase(backing)
            and read_whether_the_pool_prints(corpus, backing))


def _require_cell_citation(citation: PublishedCitation) -> StageOutputCellCitation:
    if not isinstance(citation, StageOutputCellCitation):
        raise ClaimReviewRefused(
            ["a table claim has no sentence to attack; only a cell claim is attacked"])
    return citation

