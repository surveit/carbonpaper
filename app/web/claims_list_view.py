"""The claims list: every sentence a project has proposed, in the order it is read."""
from __future__ import annotations

from collections import Counter

from pydantic import BaseModel

from app.core.ids import ID
from app.models.claims import ClaimStatus
from app.models.records.claims import Claim
from app.services import claim_evidence
from app.services.claim_shapes import read_claim_shape
from app.web.run_index import build_run_index_rows


class ClaimRow(BaseModel):
    claim_id: ID
    text: str
    value: str
    shape_label: str
    context_words: str
    stage_id: str
    run_id: ID
    status: str
    status_words: str
    href: str
    run_href: str


class ClaimsListPage(BaseModel):
    rows: list[ClaimRow]
    to_review: int
    made: int
    declined: int
    superseded: int
    # Empty where the project holds no run — there is then nothing to write a claim on.
    publish_href: str


def build_claims_list_page(project_id: ID) -> ClaimsListPage:
    held = _order_the_claims(Claim.find(created_by_project_id=project_id))
    standing = Counter(claim.status for claim in held)
    return ClaimsListPage(
        rows=[_build_claim_row(project_id, claim) for claim in held],
        to_review=standing[ClaimStatus.submitted],
        made=standing[ClaimStatus.approved],
        declined=standing[ClaimStatus.declined],
        superseded=standing[ClaimStatus.superseded],
        publish_href=_find_publish_href(project_id),
    )


def _order_the_claims(held: list[Claim]) -> list[Claim]:
    """Newest first — the clock ties, so the id settles it — then what waits is lifted."""
    newest = sorted(held, key=lambda claim: (claim.created_at, claim.id), reverse=True)
    return sorted(newest, key=lambda claim: claim.status != ClaimStatus.submitted)


def _build_claim_row(project_id: ID, claim: Claim) -> ClaimRow:
    citation = claim.citation
    shape = read_claim_shape(project_id, claim.shape_id)
    return ClaimRow(
        claim_id=claim.id,
        # A skip was refused without a sentence, so its metric label is what the row reads.
        text=claim.text or shape.label,
        value=claim_evidence.read_output_value(citation),
        shape_label=shape.label,
        context_words=_describe_context(claim),
        stage_id=citation.stage_id,
        run_id=citation.run_id,
        status=claim.status,
        status_words=_describe_status(claim.status),
        href=f"/project/{project_id}/claims/{claim.id}",
        run_href=f"/project/{project_id}/runs/{citation.run_id}",
    )


def _describe_context(claim: Claim) -> str:
    return " · ".join(f"{name} {value}" for name, value in claim.context.items())


def _describe_status(status: ClaimStatus) -> str:
    """'submitted' says who wrote it; the row says what it is waiting on."""
    return "needs review" if status == ClaimStatus.submitted else status


def _find_publish_href(project_id: ID) -> str:
    rows = build_run_index_rows(project_id)
    if not rows:
        return ""
    return f"/project/{project_id}/runs/{rows[0].run_id}/publish"
